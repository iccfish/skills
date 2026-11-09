"""Export listenable samples; only explicitly accepted, unchanged files enter the library."""
from __future__ import annotations

import hashlib
import re
import subprocess
import uuid
from pathlib import Path

import voiceprint as vp
from localio import atomic_json, read_json, file_lock
from paths import data_dir
from review import fingerprint


def root_dir(cfg) -> Path:
    """Keep samples beside the selected voiceprint library, outside skill source."""
    return (Path(cfg.vp_db).expanduser().resolve().parent if cfg.vp_db else data_dir().resolve()) / '声纹采样'


def safe_name(name: str) -> str:
    """Avoid traversal, reserved Windows basenames and collisions after sanitization."""
    return (re.sub(r'[\x00-\x1f<>:"/\\|?*]', '_', name).strip(' .')[:48] or '未命名') + '-' + fingerprint(name)[:8]


def file_hash(path: Path) -> str:
    """Bind the user's listening decision to the exact audio bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _clip(audio: Path, dst: Path, start: float, duration: float, ffmpeg: str) -> None:
    """Publish a completed MP3 without leaving truncated output after encoder failure."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    temporary = dst.with_name('.' + uuid.uuid4().hex + '.mp3')
    try:
        result = subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin',
                                 '-ss', str(start), '-i', str(audio), '-t', str(duration),
                                 '-map', '0:a:0', '-ac', '1', '-ar', '16000',
                                 '-c:a', 'libmp3lame', '-b:a', '64k', str(temporary)],
                                capture_output=True)
        if result.returncode or not temporary.is_file() or not temporary.stat().st_size:
            raise RuntimeError('声纹样本 MP3 提取失败，未发布不完整文件')
        temporary.replace(dst)
    finally:
        temporary.unlink(missing_ok=True)


def _checked_file(item: dict, root: Path, *, required=True) -> Path:
    """Validate ownership before reading or deleting any manifested clip."""
    path = Path(item['path']).resolve()
    if not path.is_relative_to(root.resolve()) or path.suffix.lower() != '.mp3':
        raise ValueError('采样文件不在配置的声纹采样目录内')
    if required and (not path.is_file() or file_hash(path) != item['sha256']):
        raise ValueError('采样文件缺失或已改变，请重新采样和试听')
    return path


def export(cfg, sentences: list, names: dict, audio: Path, manifest: Path,
           ffmpeg: str, revision: str, force: bool = False) -> dict:
    """Two nonoverlapping clips per named identity; export never writes embeddings."""
    root = root_dir(cfg)
    old = read_json(manifest)
    source_hash = file_hash(audio)
    context = fingerprint([revision, str(audio.resolve()), source_hash, str(root)])
    identity = fingerprint([context, uuid.uuid4().hex]) if force else context
    if old.get('context', old.get('identity')) == context and not force:
        for group in old['groups']:
            for item in group['files']:
                _checked_file(item, root, required=group['status'] != 'rejected')
        return old
    # Remove only obsolete pending/rejected clips. Accepted training audio remains available.
    for group in old.get('groups', []):
        if group['status'] in ('pending', 'rejected', 'insufficient'):
            for item in group['files']:
                path = _checked_file(item, root, required=False)
                if path.exists():
                    _checked_file(item, root).unlink()
    grouped = {}
    for row in sentences:
        sid = row.get('speaker_id')
        name = row.get('_manual_name') or names.get(sid) or (f'说话人{sid + 1}' if isinstance(sid, int) else '未分配')
        grouped.setdefault(name, []).append(sid)
    groups = []
    for name, ids in grouped.items():
        # Preserve all intervening other speakers to keep pick_spans from crossing boundaries.
        rows = [{**r, 'speaker_id': 0 if r.get('speaker_id') in ids else 1} for r in sentences]
        spans = vp.pick_spans(rows, 0, 2, cfg.vp_min_seconds, cfg.vp_segment_seconds) if all(isinstance(sid, int) for sid in ids) else []
        entry = {'id': str(len(groups)), 'name': name, 'speaker_ids': sorted(set(ids)),
                 'status': 'pending' if len(spans) == 2 else 'insufficient', 'files': []}
        if len(spans) == 2:
            for index, (start, duration) in enumerate(spans, 1):
                clock = f'{int(start)//3600:02d}-{int(start)//60%60:02d}-{int(start)%60:02d}'
                dst = root / safe_name(name) / f'{safe_name(name)}-{clock}-{identity[:12]}-{index}.mp3'
                _clip(audio, dst, start, duration, ffmpeg)
                entry['files'].append({'path': str(dst.resolve()), 'sha256': file_hash(dst),
                                       'start': start, 'duration': duration, 'index': index})
        groups.append(entry)
    result = {'schema': 1, 'identity': identity, 'context': context, 'revision': revision, 'audio': str(audio.resolve()),
              'audio_sha256': source_hash, 'db': str(Path(cfg.vp_db).resolve()) if cfg.vp_db else str(data_dir() / 'voiceprints.json'),
              'groups': groups}
    atomic_json(manifest, result)
    return result


def decide(cfg, manifest: Path, revision: str, decisions: dict, ffmpeg: str) -> dict:
    """Accept both clips atomically per person or discard only that person's pending files."""
    with file_lock(manifest):
        data = read_json(manifest)
        db_path = str(Path(cfg.vp_db).resolve()) if cfg.vp_db else str(data_dir() / 'voiceprints.json')
        if data.get('revision') != revision or data.get('db') != db_path:
            raise ValueError('审核/映射或声纹库已变化，请重新 --continue 并试听新样本')
        known = {g['id']: g for g in data.get('groups', [])}
        if not decisions or set(decisions) - set(known):
            raise ValueError('采样确认编号不存在，请先 --samples-list')
        for gid, accept in decisions.items():
            group = known[gid]
            if group['status'] in ('accepted', 'rejected'):
                if accept != (group['status'] == 'accepted'):
                    raise ValueError('已完成的采样决定不能反转；已入库样本请使用声纹删除命令管理')
                if not accept:
                    for item in group['files']:
                        path = _checked_file(item, root_dir(cfg), required=False)
                        if path.exists():
                            _checked_file(item, root_dir(cfg)).unlink()
                continue
            if group['status'] != 'pending' or len(group['files']) != 2:
                raise ValueError('没有两段有效样本，不能确认入库')
            files = [_checked_file(item, root_dir(cfg)) for item in group['files']]
            if accept:
                if re.fullmatch(r'说话人\d+', group['name']):
                    raise ValueError('请先用 --speaker-map 明确姓名并重新 --continue 采样，不能用占位编号入库')
                model = vp.ensure_model(cfg.vp_model_path, Path.cwd())
                digest = file_hash(model)
                db = vp.load_db(cfg.vp_db or None)
                vp.validate_db(db, digest)
                # Compute both embeddings before saving anything. Retry deduplicates by clip path.
                vectors = [vp.embed_audio(p, ffmpeg, model, 0, item['duration'])
                           for p, item in zip(files, group['files'])]
                for vec, path, item in zip(vectors, files, group['files']):
                    vp.enroll(db, group['name'], vec, str(path), 0, item['duration'], model_sha256=digest)
                vp.save_db(db, cfg.vp_db or None)
                group['status'] = 'accepted'
                try:
                    import roster
                    store = roster.load(cfg.roster_path or None)
                    roster.record(store, group['name'], data['audio'], group['id'])
                    roster.save(store, cfg.roster_path or None)
                except (OSError, ValueError, RuntimeError) as exc:
                    print(f'[roster] 声纹已保存，名单未写入，原库保留：{exc}')
            else:
                # Persist the decision first so an interrupted deletion can be retried safely.
                group['status'] = 'rejected'
                atomic_json(manifest, data)
                for path in files:
                    path.unlink(missing_ok=True)
            atomic_json(manifest, data)
        return data


def describe(data: dict) -> None:
    """Expose stable confirmation IDs and audio paths for an agent's listening questions."""
    for group in data.get('groups', []):
        print(f"[sample] {group['id']} {group['name']} 状态={group['status']}")
        for item in group['files']:
            print(f"  {item['path']}（原录音 {item['start']:.3f}s，{item['duration']:.3f}s）")
    if data.get('groups') and all(g['status'] in ('accepted', 'rejected', 'insufficient') for g in data['groups']):
        print('[report] 采样决定已处理，请按 references/report.md 阅读最终确认稿并生成单页 HTML 会议纪要。')
