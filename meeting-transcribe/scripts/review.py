"""Review-stage artifacts: immutable first draft and replayable human corrections."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from edits import fmt_ts
from localio import atomic_text, atomic_json, read_json

ATTRIBUTION_HINT = '如出现错误归属情况，按最大匹配输入，后续可手动调整'


def artifact_paths(raw: Path) -> dict:
    """Derive human-facing stages without changing legacy task manifest paths."""
    stem = raw.name[:-len('.asr.json')]
    return {key: raw.with_name(stem + suffix) for key, suffix in {
        'original': '-原始转写稿.txt', 'attributed': '-归属转写稿未确认版.txt',
        'attribution': '.attribution.json', 'reviewed': '-归属校准版.txt',
        'separated': '-说话人分离.txt', 'samples': '.samples.json',
        'report': '-会议纪要.html', 'report_meta': '.report.json',
    }.items()}


def fingerprint(value) -> str:
    """Bind approval to the exact reviewed sentences and identity mapping."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def render(sentences: list, names: dict, title: str) -> str:
    """Keep sentence indices for natural-language corrections and listening anchors."""
    lines = [title, '自动识别结果，请核对文字与发言归属；序号对应原始 JSON。', '']
    if '归属' in title:
        lines.insert(1, ATTRIBUTION_HINT)
    for i, s in enumerate(sentences):
        spk = s.get('speaker_id')
        label = s.get('_manual_name') or names.get(spk) or (f'说话人{spk + 1}' if isinstance(spk, int) else '未分配')
        lines.append(f"[{fmt_ts(s['begin_time'])}] [句{s.get('_oi', i)}] {label}：{s.get('text', '')}")
    return '\n'.join(lines) + '\n'


def original(path: Path, sentences: list) -> None:
    """Publish once; refuse to silently replace a changed first draft."""
    text = render(sentences, {}, '原始转写稿（无姓名映射）')
    if path.exists():
        if path.read_text(encoding='utf-8') != text:
            raise ValueError('原始转写稿与 ASR 不一致，已保留文件，请先核对')
    else:
        atomic_text(path, text, overwrite=False)


def attributed(paths: dict, sentences: list, names: dict | None = None,
               suggestions: dict | None = None) -> dict | None:
    """Freeze the first identity mapping; later corrections only affect the calibrated draft."""
    path = paths['attribution']
    record = read_json(path)
    raw_revision = fingerprint(sentences)
    if record:
        if record.get('schema') != 1 or record.get('raw_revision') != raw_revision:
            raise ValueError('首次归属快照与原始转写不一致，已保留文件，请先核对')
        frozen = {int(k): v for k, v in record['names'].items()}
        text = render(sentences, frozen, '归属转写稿未确认版（首次归属快照）')
        if record.get('text_sha256') != hashlib.sha256(text.encode('utf-8')).hexdigest():
            raise ValueError('首次归属快照损坏，不能猜测映射或覆盖未确认版')
    else:
        if paths['attributed'].exists():
            raise ValueError('未确认版存在但缺少首次归属快照，已保留文件，请先核对')
        if names is None:
            return None
        text = render(sentences, names, '归属转写稿未确认版（首次归属快照）')
        record = {'schema': 1, 'raw_revision': raw_revision,
                  'names': {str(k): v for k, v in names.items()},
                  'suggestions': {str(k): v for k, v in (suggestions or {}).items()},
                  'text_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}
        atomic_json(path, record, overwrite=False)
    if paths['attributed'].exists():
        if paths['attributed'].read_bytes() != text.encode('utf-8'):
            raise ValueError('未确认版被修改，已保留文件；请通过校准记录调整，不覆盖首次版本')
    else:
        atomic_text(paths['attributed'], text, overwrite=False)
    return record
