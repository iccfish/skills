"""会议转写入口：音频准备、可恢复云任务、不可变原始结果、纠错重放、人工命名和原子导出。
工具链作为常规前置始终检查；声纹是可选增强，不代替用户确认。"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bailian  # noqa: E402
import edits  # noqa: E402
import media as media_mod  # noqa: E402
import roster  # noqa: E402
import storage  # noqa: E402
import jobs  # noqa: E402
import review  # noqa: E402
import sampling  # noqa: E402
from localio import atomic_json, atomic_text, read_json, file_lock  # noqa: E402
import voiceprint as vp  # noqa: E402
from config import ensure_tools, find_tools, load_config, save_vp_switch  # noqa: E402
from edits import fmt_ts  # noqa: E402  （时间戳格式化统一由 edits 提供，避免两处各写一份）


class VoiceprintPending(RuntimeError):
    """Cloud output is safe; initial attribution needs the user's dependency choice."""


def speaker_order(sentences: list) -> list:
    """按首次出现顺序返回 speaker_id 列表。"""
    seen = []
    for s in sentences:
        spk = s.get("speaker_id")
        if spk is not None and spk not in seen:
            seen.append(spk)
    return seen


def load_existing_map(base: Path) -> dict:
    """读取同主名 .meta.json 里已确认的说话人映射，返回 {speaker_id: 姓名}。

    用于 --from-json 重跑但未传 --speaker-map 时自动沿用旧姓名，
    避免「重生成一次就把已确认的名字冲掉」这种静默丢数据。
    新版 meta 的姓名字段已由 edits 记录重放得出，这里只兜底旧文件。
    """
    # overwrite=True：取主名路径本身，否则已存在时会被改写成 -1 而读不到
    path = media_mod.build_output_path(base, ".meta.json", overwrite=True)
    data = read_json(path)
    mapping = data.get("speaker_map") or {}
    result = {}
    for k, v in mapping.items():
        try:
            result[int(k)] = v
        except (TypeError, ValueError):
            continue
    return result


def summarize(sentences: list) -> tuple:
    """汇总转写结果，返回 (per-speaker 统计, 连续段落, 总时长毫秒)。

    段落指同一说话人连续发言合并后的一块；统计里含句数、字数、首次发言时间、段数。
    txt 与 .meta.json 共用这份结果，避免两处口径不一致。
    """
    stats: dict = {}
    blocks: list = []
    for s in sentences:
        spk = s.get("speaker_id")
        st = stats.setdefault(spk, {"n": 0, "chars": 0, "first": s["begin_time"], "blocks": 0})
        st["n"] += 1
        st["chars"] += len(s.get("text", ""))
        if not blocks or blocks[-1][0] != spk:
            blocks.append([spk, s["begin_time"], [s.get("text", "")]])
            st["blocks"] += 1
        else:
            blocks[-1][2].append(s.get("text", ""))

    total_ms = max((s.get("end_time", 0) for s in sentences), default=0)
    return stats, blocks, total_ms


def build_meta(sentences: list, names: dict, meta: dict) -> dict:
    """构造 .meta.json 的内容：映射关系、每人统计与运行时信息。

    schema 字段用于后续兼容；edits 段保存人工调整的完整记录与归一化视图，
    --from-json 重新生成时据此重放，不用用户再纠一遍。
    """
    stats, blocks, total_ms = summarize(sentences)
    order = speaker_order(sentences)
    return {
        "schema": 2,
        "source": meta.get("source", ""),
        "audio": meta.get("audio", ""),
        "model": meta.get("model", ""),
        "diarization": meta.get("diarization", True),
        "duration_ms": total_ms,
        "sentences": len(sentences),
        "blocks": len(blocks),
        "raw_json": meta.get("json_name", ""),
        "outputs": meta.get("outputs", {}),
        "generated": meta.get("generated", ""),
        "speaker_map": {str(k): names.get(k, f"说话人{k + 1}") for k in order},
        "speakers": [
            {
                "id": k,
                "name": names.get(k, f"说话人{k + 1}"),
                "sentences": stats[k]["n"],
                "chars": stats[k]["chars"],
                "blocks": stats[k]["blocks"],
                "first_ms": stats[k]["first"],
                "first_ts": fmt_ts(stats[k]["first"]),
            }
            for k in order
        ],
        "edits": meta.get("edits") or {"version": edits.EDIT_VERSION, "count": 0,
                                       "adjustments": [], "summary": {}},
        "extra": meta.get("extra", {}),
    }


# 可用于声纹抽取的音频/视频扩展名白名单
MEDIA_EXTS = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus",
              ".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv")


def _is_media(path: Path) -> bool:
    """判断路径是否为可解码的媒体文件，用于排除 .json / .txt 这类同名产物。"""
    return path.is_file() and path.suffix.lower() in MEDIA_EXTS


def resolve_audio_for_vp(base: Path, known: Path | None = None) -> Path | None:
    """定位用于声纹抽取的音频文件。

    --from-json 时手上只有结果 JSON，需反查音频：先找同主名的媒体文件
    （转码产物 .mp3 或原始录音），再退回 .meta.json 记录的 audio 字段。
    必须校验扩展名——--from-json 生成的 meta 里 audio 记的是结果 JSON 自身，
    直接拿去解码会失败。找不到返回 None，调用方据此跳过声纹环节。
    """
    if known and _is_media(known):
        return known

    for ext in MEDIA_EXTS:
        cand = media_mod.build_output_path(base, ext, overwrite=True)
        if _is_media(cand):
            return cand

    try:
        meta = json.loads(media_mod.build_output_path(base, ".meta.json", overwrite=True)
                          .read_text(encoding="utf-8"))
        audio = Path(meta.get("audio", ""))
        if _is_media(audio):
            return audio
    except (OSError, ValueError):
        pass
    return None


def run_voiceprint(cfg, sentences: list, audio_path: Path | None, base: Path,
                   ffmpeg: str, enabled: bool) -> tuple:
    """对每个说话人抽声纹并匹配历史库，返回 (建议表, 向量表, 音频路径)。

    任何环节失败都只打印原因并返回空结果——声纹是增强项，不能影响转写主流程。
    """
    if not enabled:
        print("[vp] 声纹识别已关闭")
        return {}, {}, None

    ok, reason = vp.available()
    if not ok:
        print(f"[vp] 声纹不可用，跳过：{reason}")
        return {}, {}, None

    try:
        audio = resolve_audio_for_vp(base, audio_path)
        if not audio:
            print("[vp] 未找到可用音频，跳过声纹匹配")
            return {}, {}, None

        model_path = vp.ensure_model(cfg.vp_model_path, Path.cwd())
        db = vp.load_db(cfg.vp_db or None)
        import hashlib
        fingerprint = hashlib.sha256(model_path.read_bytes()).hexdigest()
        vp.validate_db(db, fingerprint)

        suggestions, vp_data = {}, {}
        for spk in speaker_order(sentences):
            spans = vp.pick_spans(sentences, spk, cfg.vp_segments,
                                  cfg.vp_min_seconds, cfg.vp_segment_seconds)
            if not spans:
                print(f"[vp] speaker {spk} 无足够长度的发言片段，跳过")
                continue
            multi = [vp.embed_audio(audio, ffmpeg, model_path, s, d) for s, d in spans]
            vec = vp.average_embedding(multi)
            # 一并保留片段信息：入库时用它做去重键，避免同一段重复堆积
            vp_data[spk] = {
                "vec": vec,
                "start": spans[0][0],
                "duration": round(sum(d for _, d in spans), 3),
                "segments": len(spans),
                "model_sha256": fingerprint,
            }
            suggestions[spk] = vp.match(db, vec, cfg.vp_threshold, model_sha256=fingerprint)
        return suggestions, vp_data, audio
    except Exception as exc:
        # 声纹失败不应中断转写：记录原因，主流程继续
        print(f"[vp] 声纹环节出错，已跳过（不影响转写）：{exc}")
        return {}, {}, None


def initial_attribution(cfg, sentences: list, audio_path: Path | None,
                        base: Path, ffmpeg: str, args) -> dict:
    """Match immediately after ASR once; freeze the result independently of calibration edits."""
    stages = review.artifact_paths(base)
    record = review.attributed(stages, sentences)
    if record is not None:
        return record
    decided = resolve_vp_switch(cfg, Path.cwd(), allow_prompt=True)
    if decided is None:
        print_vp_pending(voiceprint_readiness(cfg, Path.cwd()))
        raise VoiceprintPending('原始结果已保存，首次归属等待声纹开关选择；可从本地 JSON 继续，无需重新转写')
    suggestions, _, _ = run_voiceprint(cfg, sentences, audio_path, base, ffmpeg, decided)
    names = {}
    for sid in speaker_order(sentences):
        candidates = suggestions.get(sid) or []
        names[sid] = candidates[0][0] if candidates and candidates[0][2] else f'说话人{sid + 1}'
        print(f'[identity] speaker {sid} -> {names[sid]}（首次暂定归属，不代表每句归属均正确）')
        if candidates:
            print('[vp] 候选：' + '、'.join(f'{n} {score:.4f}' for n, score, _ in candidates[:3]))
    record = review.attributed(stages, sentences, names, suggestions)
    print(f"[out] 归属转写稿未确认版: {stages['attributed']}")
    print(f'[identity] {review.ATTRIBUTION_HINT}')
    try:
        known = roster.known_names(roster.load(args.roster))
    except (OSError, ValueError, RuntimeError):
        known = []
    if known:
        print('[identity] 历史姓名候选：' + '、'.join(known))
    # Give the agent verbatim, indexed evidence when the library cannot name a cluster.
    for sid, name in names.items():
        if name != f'说话人{sid + 1}':
            continue
        print(f'[identity] 请确认 speaker {sid} 主要是哪位；{review.ATTRIBUTION_HINT}')
        chars = 0
        for i, row in enumerate(sentences):
            if row.get('speaker_id') == sid:
                print(f"  [{fmt_ts(row['begin_time'])}] [句{i}] {row.get('text', '')}")
                chars += len(row.get('text', ''))
                if chars >= cfg.sample_chars:
                    break
    return record


def print_edits(base: Path, sentences: list) -> None:
    """打印该文件已记录的人工调整：原始记录 + 归一化后的净效果。

    只读，不写回任何文件；用于核对「重放后到底改了什么」，
    以及判断哪些记录因结果变化而失效（失效记录会原样保留在 meta 里）。
    """
    adjustments = edits.load(media_mod.build_output_path(base, ".meta.json", overwrite=True))
    if not adjustments:
        print("[edit] 暂无人工调整记录")
        return

    before = edits.snapshot(edits.tag_origins(sentences))
    result = edits.apply(sentences, adjustments)
    summary = edits.summarize(before, sentences, result, total=len(adjustments))

    print(f"=== 人工调整记录：{len(adjustments)} 条 ===")
    for i, rec in enumerate(adjustments, 1):
        print(f"  {i:>3}. [{rec['id']}] {edits.describe_record(rec)}")

    print("\n--- 重放后的净效果 ---")
    print(f"  姓名：{summary['renames'] or '（无）'}")
    for s in summary["splits"]:
        print(f"  句中摘段：[{s['ts']}] 句 {s['idx']} 切成 {s['parts']} 段，"
              f"归属 {s['speakers']}")
    for m in summary["merges"]:
        print(f"  整位合并：speaker {m['from']} → speaker {m['to']}（全部 {m['sentences']} 句）")
    for m in summary["moves"]:
        print(f"  逐句改判：[{m['ts']}] 句 {m['idx']} speaker {m['from']} → {m['to']}")
    for m in summary["texts"]:
        print(f"  文本修正：[{m['ts']}] 句 {m['idx']} {m['old'][:20]!r} → {m['new'][:20]!r}")
    for s in summary["skipped"]:
        print(f"  [失效] {s['reason']}：{edits.describe_record(s['record'])}")
    for w in summary["warnings"]:
        print(f"  [部分] {w['reason']}：{edits.describe_record(w['record'])}")
    for c in summary["conflicts"]:
        print(f"  [冲突] {c['reason']}：{edits.describe_record(c['record'])}")


def find_sentences(sentences: list, keyword: str, names: dict, limit: int = 40) -> None:
    """按关键词检索句子，打印序号/时间/说话人，便于定位 --reassign 的目标句。"""
    hits = [(i, s) for i, s in enumerate(sentences) if keyword in s.get("text", "")]
    print(f"=== 命中 {len(hits)} 句（关键词：{keyword}）===")
    for i, s in hits[:limit]:
        spk = s.get("speaker_id")
        who = names.get(spk, f"说话人{spk + 1}") if spk is not None else "未知"
        print(f"  {i:>4} [{fmt_ts(s.get('begin_time'))}] {who}：{s.get('text', '')}")
    if len(hits) > limit:
        print(f"  ...另有 {len(hits) - limit} 句未显示（用更具体的关键词缩小范围）")


def process_file(cfg, src: Path, ffmpeg: str, ffprobe: str, args) -> None:
    """Coordinate one output group so two processes cannot edit it simultaneously."""
    if args.resume:
        group, state = jobs.load(Path(args.resume), cfg)
    elif args.from_json:
        group, state = media_mod.output_group(Path(args.from_json), existing=True), None
    else:
        # Preparation may fail without leaving a cloud job or a reserved artifact group.
        audio, _ = media_mod.prepare_audio(ffmpeg, ffprobe, src)
        if args.dry_run:
            print(f"[dry-run] 音频准备完成: {audio}；已跳过上传与转写")
            return
        group, state = jobs.reserve(src, cfg, not args.no_diarization, args.keep_oss)
        state["audio"] = str(audio.resolve())
    with file_lock(group["meta"].with_name(group["meta"].name + ".processing")):
        _process_file(cfg, src, ffmpeg, ffprobe, args, group, state,
                      audio if not args.resume and not args.from_json else None)


def _process_file(cfg, src: Path, ffmpeg: str, ffprobe: str, args,
                  group: dict, state: dict | None, prepared: Path | None) -> None:
    """Preserve raw ASR, replay edits and atomically regenerate the associated outputs."""
    print(f"\n=== 处理: {src.name} ===")

    # --from-json 分支没有音频产物，留空交由 resolve_audio_for_vp 反查
    audio_path: Path | None = None

    old_meta = read_json(group["meta"])
    if old_meta.get("raw_json") and old_meta["raw_json"] != group["json"].name:
        raise ValueError("元数据关联的原始 JSON 与当前文件不一致，请先核对整组产物")
    if args.from_json:
        data = read_json(group["json"])
        audio_label = old_meta.get("audio") or old_meta.get("source") or str(group["json"])
        base = group["json"]
    else:
        audio_path = prepared or (Path(state["audio"]) if state.get("audio") else None)
        data = jobs.execute(cfg, group["task"], state, group, audio_path)
        audio_label = state["source"]
        base = group["json"]

    sentences = data["transcripts"][0]["sentences"]
    print(f"[asr] 识别完成：{len(sentences)} 句")

    # Cloud results were persisted before cleanup; regeneration never rewrites the raw source.
    json_path = group["json"]
    print(f"[out] 原始 JSON: {json_path.name}")

    # 人工调整从原始句重放；首次归属已冻结，采样必须使用校准后的身份与时间。
    # 原始 JSON 已按 ASR 原样落盘，重放只改内存，因此每次都从同一份原始结果重新推导。
    # 打「原始句序号」标记后再取快照：拆句会让句子数量变化，用下标对齐必然错位。
    review.original(review.artifact_paths(group["json"])["original"], sentences)
    attribution = initial_attribution(cfg, sentences, audio_path, base, ffmpeg, args)
    initial_names = {int(k): v for k, v in attribution['names'].items()}
    before = edits.snapshot(edits.tag_origins(sentences))
    meta_existing = group["meta"]
    loaded = edits.load(meta_existing) if args.from_json or args.resume else []
    if args.clear_edits:
        print(f"[edit] 已清空 {len(loaded)} 条历史调整记录（本轮的调整仍会记录）")
        loaded = []
    if args.undo_edit:
        loaded = edits.append_undo(loaded, args.undo_edit)

    # 姓名 → speaker_id 反查表：让 --reassign / --split 能直接写「是李四说的」，
    # 不必先查 id。来源是已记录的映射 + 本轮 --speaker-map（后者优先，可当场给新名字）。
    existing_names = {} if args.clear_edits or args.undo_edit else load_existing_map(base)
    name_to_id = {name: spk for spk, name in existing_names.items() if name}
    name_to_id.update({name: spk for spk, name
                       in edits.parse_speaker_map(",".join(args.speaker_map or [])).items() if name})

    adjustments = edits.dedupe(loaded, edits.from_cli(
        sentences,
        renames=",".join(args.speaker_map or []),
        reassign=args.reassign,
        merges=args.merge,
        texts=args.text,
        splits=args.split,
        names=name_to_id,
    ))
    adjustments = edits.dedupe(adjustments, edits.review_records(sentences, args.attribute, args.replace_text))
    edit_result = edits.apply(sentences, adjustments)
    edit_summary = edits.summarize(before, sentences, edit_result, total=len(adjustments))

    if edits.describe(edit_summary):
        print(f"[edit] 已应用人工调整：{edits.describe(edit_summary)}")
    for item in edit_result["skipped"]:
        print(f"[edit] 跳过一条记录：{item['reason']}", file=sys.stderr)
    for item in edit_result["warnings"]:
        print(f"[edit] 部分生效：{item['reason']}", file=sys.stderr)
    for item in edit_result["conflicts"]:
        print(f"[edit] 记录冲突：{item['reason']}", file=sys.stderr)

    stages = review.artifact_paths(group["json"])
    manual_names = dict(edit_result["names"])
    # Legacy confirmed mappings remain usable; new automatic suggestions never become manual locks.
    if not old_meta.get("workflow") and not args.clear_edits and not args.undo_edit:
        manual_names = {**load_existing_map(base), **manual_names}
    manual_names = {k: v for k, v in manual_names.items() if v != f"说话人{k + 1}"}
    review_revision = review.fingerprint([sentences, manual_names, initial_names])
    info = {
        "source": state["source"] if state else old_meta.get("source", str(base)),
        "audio": state.get("audio", audio_label) if state else old_meta.get("audio", audio_label),
        "model": state["model"] if state else old_meta.get("model", cfg.model),
        "diarization": state["diarization"] if state else old_meta.get("diarization", not args.no_diarization),
        "json_name": json_path.name, "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "edits": edits.block(adjustments, edit_summary), "edits_line": edits.describe(edit_summary),
        "outputs": {**{k: v.name for k, v in group.items()}, **{k: v.name for k, v in stages.items()}},
    }
    decisions = {str(k): True for k in args.samples_accept}
    for k in args.samples_reject:
        if str(k) in decisions:
            raise ValueError('同一人不能同时确认和拒绝采样')
        decisions[str(k)] = False
    if decisions or args.samples_list:
        workflow = old_meta.get('workflow', {})
        if workflow.get('review_revision') != review_revision or workflow.get('stage') != 'separated':
            raise ValueError('审核稿已变化或尚未继续处理，请先 --continue 生成并试听样本')
        data = (sampling.decide(cfg, stages['samples'], workflow['sample_revision'], decisions, ffmpeg)
                if decisions else read_json(stages['samples']))
        sampling.describe(data)
        return

    names = {s: manual_names.get(s, initial_names.get(s, f'说话人{s + 1}')) for s in speaker_order(sentences)}
    workflow = {'stage': 'calibrated' if adjustments else 'attributed',
                'review_revision': review_revision, 'manual_names': {str(k): v for k, v in manual_names.items()},
                'attribution_revision': review.fingerprint(attribution)}
    reviewed_text = review.render(sentences, names, '归属校准版') if adjustments or stages['reviewed'].exists() else None
    if args.continue_processing:
        workflow['stage'] = 'separated'
        workflow['sample_revision'] = review.fingerprint([review_revision, names])
        text = review.render(sentences, names, '说话人分离（姓名建议须试听采样确认）')
        atomic_text(stages['separated'], text)
        # Save the separated text/meta before optional sampling so failures remain recoverable.
        document = build_meta(sentences, names, info)
        document['workflow'] = workflow
        atomic_json(group['meta'], document)
        if reviewed_text is not None:
            atomic_text(stages['reviewed'], reviewed_text)
        print(f"[out] 说话人分离: {stages['separated']}")
        audio = resolve_audio_for_vp(base, audio_path)
        if audio is None:
            raise ValueError('找不到原音频，分离稿已保存，未采样或入库')
        data = sampling.export(cfg, sentences, names, audio, stages['samples'], ffmpeg, workflow['sample_revision'], force=args.resample)
        sampling.describe(data)
        print('[review] 请逐人试听两段样本，询问「准确 / 不对或不确定」；只有准确才用 --samples-accept 入库')
    else:
        document = build_meta(sentences, names, info)
        document['workflow'] = workflow
        atomic_json(group['meta'], document)
        if reviewed_text is not None:
            atomic_text(stages['reviewed'], reviewed_text)
            print(f"[out] 归属校准版: {stages['reviewed']}")
        print(f"[out] 归属转写稿未确认版: {stages['attributed']}")
        print('[review] 请审核姓名、文字和前后归属，按句索引修改并累计到归属校准版；未确认版保持不变。确认后 --continue 才生成试听采样，不自动入库。')


def voiceprint_readiness(cfg, project_dir: Path) -> dict:
    """检查声纹运行条件，返回 {deps_missing, model, ready}。

    只做检查，不安装、不下载，因此可安全地在流程最前面调用。
    ready 表示依赖包与模型都齐备，与开关本身的状态无关。
    """
    import importlib.util

    deps_missing = [m for m in ("numpy", "onnxruntime", "kaldi_native_fbank") if importlib.util.find_spec(m) is None]
    model = vp.locate_model(cfg.vp_model_path, project_dir)
    return {"deps_missing": deps_missing, "model": model,
            "ready": not deps_missing and model is not None}


def describe_vp_needs(state: dict) -> list:
    """把待补项整理成人类可读的清单，供自检输出与智能体提问复用。"""
    needs = []
    if state["deps_missing"]:
        needs.append(f"安装 Python 包 {'、'.join(state['deps_missing'])}（约 28 MB）")
    if not state["model"]:
        needs.append(f"下载声纹模型 {vp.MODEL_FILENAME}（26.9 MiB）")
    return needs


def ensure_vp_ready(cfg, project_dir: Path) -> bool:
    """开关为 true 时补齐运行条件：缺包装包、缺模型下模型，返回是否可用。

    任一环节失败都只降级为「本次不用声纹」，不中断转写——否则一次网络抖动
    就会让整场会议录音白跑。
    """
    state = voiceprint_readiness(cfg, project_dir)

    if state["deps_missing"]:
        print(f"[vp] 开关为开启，缺少依赖：{'、'.join(state['deps_missing'])}，开始安装")
        if install_deps() != 0:
            print("[vp] 依赖安装失败，本次按关闭处理（转写不受影响）")
            return False
        # voiceprint 模块在 import 时已把缺失依赖记为不可用，装完必须重载才生效
        import importlib
        importlib.reload(vp)
        state = voiceprint_readiness(cfg, project_dir)
        if state["deps_missing"]:
            print(f"[vp] 依赖仍缺失：{'、'.join(state['deps_missing'])}，本次按关闭处理")
            return False

    if not state["model"]:
        print("[vp] 开关为开启，模型缺失，开始下载")
        try:
            vp.ensure_model(cfg.vp_model_path, project_dir)
        except Exception as exc:
            print(f"[vp] 模型下载失败，本次按关闭处理（转写不受影响）：{exc}")
            return False

    return True


def resolve_vp_switch(cfg, project_dir: Path, allow_prompt: bool) -> bool | None:
    """流程开始前确定本次是否启用声纹，返回 True/False；返回 None 表示需用户决定。

    - 开关为 false：直接关闭，不检查依赖、不联网
    - 开关为 true：缺什么补什么，补不上就降级关闭
    - 开关为空：依赖与模型齐备则自动置 true；否则返回 None，由智能体向用户提问
    """
    if cfg.vp_enabled is False:
        print("[vp] 开关：关闭（vp_enabled=false 或 --no-voiceprint）")
        return False

    if cfg.vp_enabled is True:
        print("[vp] 开关：启用（vp_enabled=true 或 --voiceprint）")
        return ensure_vp_ready(cfg, project_dir)

    # 未设置：先看环境能否直接满足，能就不打扰用户
    state = voiceprint_readiness(cfg, project_dir)
    if state["ready"]:
        print("[vp] 开关未设置，依赖与模型均已就绪 → 自动启用")
        return True
    if not allow_prompt:
        print("[vp] 开关未设置且条件不齐，本次按关闭处理（可用 --voiceprint 强制启用）")
        return False
    return None


def print_vp_pending(state: dict) -> None:
    """输出待决策信息，供智能体向用户提问；此时尚未开始转写，不产生任何费用。"""
    print("=== 声纹识别待决策 ===")
    print("配置里的 vp_enabled 为空（未设置），且当前环境不满足运行条件：")
    for need in describe_vp_needs(state):
        print(f"  - 需{need}")
    print("启用后：用声纹建议说话人身份；导出两段采样，用户试听确认准确后才入库")
    print("关闭后：仅按人工辨认命名，转写结果完全不受影响")
    print("请选择：--voiceprint 启用 / --no-voiceprint 关闭，"
          "或用 --vp-switch on|off 写入配置")
    print("=== 尚未开始转写，未产生任何费用 ===")


def check_env(cfg, project_dir: Path) -> int:
    """打印环境自检报告，返回退出码（0=转写链路可用，1=缺关键项）。

    只做检查，不下载模型、不发网络请求，可离线运行。声纹依赖缺失属「降级」而非「失败」，
    因此不影响退出码，只在结论里单独提示。
    """
    import importlib.util
    import shutil

    print("=== 会议转写技能 · 环境自检 ===\n")

    # Python 解释器：声纹依赖装在这个解释器下才有效，换解释器会失效
    print(f"[python] {sys.version.split()[0]}  {sys.executable}")

    # ffmpeg / ffprobe：优先看配置目录，其次 PATH
    discovered = find_tools(cfg, project_dir)
    tools_ok = all(discovered)
    for name, found in zip(("ffmpeg", "ffprobe"), discovered):
        print(f"[{name}] {'✓ ' + str(found) if found else '✗ 未找到，请安装完整工具链或用 FFMPEG_DIR 指定目录'}")

    # 可选依赖：缺失只关闭声纹，不影响转写
    missing_mods = [m for m in ("numpy", "onnxruntime", "kaldi_native_fbank") if importlib.util.find_spec(m) is None]
    for m in ("numpy", "onnxruntime", "kaldi_native_fbank"):
        if m in missing_mods:
            print(f"[dep] {m:<12} ✗ 未安装")
        else:
            try:
                from importlib.metadata import version
                print(f"[dep] {m:<12} ✓ {version(m)}")
            except Exception:
                print(f"[dep] {m:<12} ✓ 已安装")

    # 开关状态单列一行：三态中的「未设置」与「已关闭」行为不同，不能混为一谈
    switch = {True: "启用（vp_enabled=true）", False: "关闭（vp_enabled=false）",
              None: "未设置（留空，流程开始时按环境判定或询问）"}[cfg.vp_enabled]
    print(f"[switch] {switch}")

    # 模型：只定位不下载，因此「缺失」在自检里是正常状态
    model = vp.locate_model(cfg.vp_model_path, project_dir)
    if model:
        print(f"[model] ✓ {model} ({model.stat().st_size / 1048576:.1f} MiB)")
    else:
        print("[model] - 未找到，首次使用将下载到用户缓存")

    db = vp.load_db(cfg.vp_db or None)
    speakers = db.get("speakers", [])
    print(f"[vdb] {'✓' if speakers else '-'} {len(speakers)} 人 / "
          f"{sum(len(s.get('samples', [])) for s in speakers)} 样本  "
          f"{Path(cfg.vp_db).name if cfg.vp_db else 'voiceprints.json'}")

    # 云端凭据：只报是否齐全，Key 脱敏到后 4 位
    key = cfg.api_key
    if key:
        tail = key[-4:] if len(key) > 4 else "****"
        print(f"[bailian] ✓ API Key 已配置（****{tail}），模型 {cfg.model}")
    else:
        print("[bailian] ✗ 缺 API Key（BAILIAN_API_KEY 或配置文件 api_key）")
    oss_missing = cfg.missing(["oss_endpoint", "oss_access_key_id",
                               "oss_access_key_secret", "oss_bucket"])
    print(f"[oss] {'✓ 4/4 已配置' if not oss_missing else '✗ 缺 ' + '/'.join(oss_missing)}")

    # 结论：转写链路与声纹分别给出状态，避免把「声纹降级」误读成「技能坏了」
    print("")
    problems = []
    if not tools_ok:
        problems.append("ffmpeg/ffprobe 不可用（转写必须先有它）")
    if not key:
        problems.append("百炼 API Key 缺失")
    if oss_missing:
        problems.append("OSS 配置不全")
    if problems:
        print("[结论] 转写链路不可用：")
        for p in problems:
            print(f"       - {p}")
    else:
        print("[结论] 转写链路可用")

    if cfg.vp_enabled is False:
        print("[结论] 声纹已按配置关闭（vp_enabled=false），下面的依赖/模型状态不影响转写")
    elif missing_mods:
        print(f"[结论] 声纹不可用（已降级为纯人工命名）：缺 {'、'.join(missing_mods)}")
        print(f"       修复：{sys.executable} -m pip install -r "
              f"{Path(__file__).resolve().parent.parent / 'requirements.txt'}")
        print("       或直接运行：python scripts/transcribe.py --install-deps")
    elif not model:
        print("[结论] 声纹可用，模型将在首次使用时自动下载")
    else:
        print("[结论] 声纹可用")

    return 0 if not problems else 1


def install_deps() -> int:
    """安装声纹所需的可选依赖（pip install -r requirements.txt），返回子进程退出码。

    只装声纹依赖，不碰系统其他包；失败时原样透出 pip 输出，便于判断是网络还是编译问题。
    """
    import subprocess

    req = Path(__file__).resolve().parent.parent / "requirements.txt"
    if not req.is_file():
        print(f"[deps] 找不到依赖清单: {req}", file=sys.stderr)
        return 2
    print(f"[deps] 安装: {req}")
    print(f"[deps] 解释器: {sys.executable}")
    code = subprocess.call([sys.executable, "-m", "pip", "install", "-r", str(req)])
    if code == 0:
        print("[deps] 完成。用 --check-env 复核。")
    else:
        print(f"[deps] pip 退出码 {code}，安装未完成", file=sys.stderr)
    return code


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="会议转写：原始稿 -> 首次暂定归属 -> 按句校准 -> 明确继续分离及采样 -> 试听确认入库")
    parser.add_argument("inputs", nargs="*", help="一个或多个媒体文件（音频或视频）")
    parser.add_argument("--config", default=None, help="指定配置文件路径")
    parser.add_argument("--continue", dest="continue_processing", action="store_true", help="明确继续：沿用首次归属及校准，生成分离稿和两段采样；不自动入库")
    parser.add_argument("--resample", action="store_true", help="与 --continue 同用，重新生成待确认样本；保留已入库的旧音频")
    parser.add_argument("--attribute", action="append", default=[], help="仅给片段指定姓名：句序号=起点..终点=姓名；.. 表示整句")
    parser.add_argument("--replace-text", action="append", default=[], help="逐字替换并保留其他字时间：句序号=原文=>新文")
    parser.add_argument("--samples-list", action="store_true", help="列出本次采样状态及音频路径")
    parser.add_argument("--samples-accept", action="append", default=[], metavar="采样组ID", help="用户试听确认准确后，录入该组两段样本")
    parser.add_argument("--samples-reject", action="append", default=[], metavar="采样组ID", help="不对或不确定：删除该组待确认样本，不入库")
    parser.add_argument("--from-json", dest="from_json", default=None,
                        help="已有转写结果 JSON，默认只审核；显式 --continue 才做分离及采样")
    parser.add_argument("--resume", metavar="TASK_JSON", help="恢复已有任务或重试清理，不重新提交转写")
    parser.add_argument("--undo-edit", action="append", default=[], metavar="ID_OR_NUMBER",
                        help="撤销指定调整记录并从原始结果重放；先用 --show-edits 查看编号")
    parser.add_argument("--reassign", "--reassign-range", dest="reassign", action="append",
                        default=[], metavar="IDX[,IDX..]=SPK | START-END=SPK",
                        help="人工调整：把指定句（序号从 0 起）或时间区间内的句子改判给 SPEAKER")
    parser.add_argument("--merge", action="append", default=[], metavar="FROM=TO",
                        help="人工调整：把 speaker FROM 整体并入 speaker TO（分离把同一人拆成两个时用）")
    parser.add_argument("--split", "--carve", dest="split", action="append", default=[],
                        metavar="LOC=起点..终点=SPK",
                        help="人工调整：把句中「起点..终点」这段摘出来改判给 SPK（前后仍归原说话人）；"
                             "省略起点=从句首，省略终点=到句尾，只给起点即从该处切开")
    parser.add_argument("--text", action="append", default=[], metavar="IDX=文本",
                        help="人工调整：修正第 IDX 句的文本")
    parser.add_argument("--clear-edits", action="store_true",
                        help="清空该文件已记录的人工调整（本轮通过命令行给出的调整仍会记录）")
    parser.add_argument("--show-edits", action="store_true",
                        help="只读：打印已记录的人工调整与重放后的净效果（配合 --from-json）")
    parser.add_argument("--find", metavar="关键词",
                        help="只读：在结果 JSON 里检索句子，打印序号/时间/说话人（配合 --from-json）")
    parser.add_argument("--speaker-map", action="append", default=[],
                        help="预设说话人映射，如 \"0=张三,1=李四\"（可重复）")
    parser.add_argument("--samples", type=int, default=None,
                        help="展示每个说话人的样例句数上限（安全兜底，默认 30 句）")
    parser.add_argument("--sample-chars", type=int, default=None,
                        help="展示每个说话人的样例字数预算（默认 200 字）")
    parser.add_argument("--roster", default=None,
                        help="说话人历史名单路径（默认用户数据目录）")
    parser.add_argument("--list-speakers", action="store_true",
                        help="打印说话人历史名单后退出")
    parser.add_argument("--no-diarization", action="store_true", help="关闭说话人分离")
    parser.add_argument("--dry-run", action="store_true", help="只做音频准备，不上传不调用")
    parser.add_argument("--keep-oss", action="store_true",
                        help="保留 OSS 上的临时音频（默认转写结束后自动删除；排障用）")
    parser.add_argument("--no-voiceprint", action="store_true",
                        help="关闭声纹匹配及入库；仍可生成分离稿和试听采样")
    parser.add_argument("--voiceprint", action="store_true",
                        help="强制启用声纹（覆盖配置里的关闭/未设置；缺依赖会自动补）")
    parser.add_argument("--vp-switch", choices=["on", "off", "auto"], default=None,
                        help="把声纹开关写入配置：on=启用，off=关闭，auto=恢复未设置")
    parser.add_argument("--vp-threshold", type=float, default=None,
                        help="声纹命中阈值（默认 0.74，换场景需重新校准）")
    parser.add_argument("--vp-db", default=None, help="声纹库路径（默认用户数据目录）")
    parser.add_argument("--vp-model", default=None, help="声纹 ONNX 模型路径")
    # 声纹管理子命令：显式参数而非 REMAINDER，避免与位置参数 inputs 争抢解析
    parser.add_argument("--vp-list", action="store_true", help="声纹管理：列出当前声纹")
    parser.add_argument("--vp-query", metavar="姓名", help="声纹管理：查询指定姓名的声纹")
    parser.add_argument("--vp-compute", metavar="音频",
                        help="声纹管理：计算指定音频的声纹并打印候选")
    parser.add_argument("--vp-confirmed-audio", action="store_true", help="手工录入前已试听确认身份；不能由智能体自行假定")
    parser.add_argument("--vp-enroll", metavar="姓名=音频",
                        help="声纹管理：把音频的声纹录入到该姓名下")
    parser.add_argument("--vp-remove", metavar="姓名", help="声纹管理：删除该姓名的声纹")
    parser.add_argument("--vp-index", type=int, default=None,
                        help="声纹管理：配合 --vp-remove 只删第 N 条样本")
    parser.add_argument("--vp-start", type=float, default=None,
                        help="声纹管理：起始秒（--vp-compute / --vp-enroll，默认 0）")
    parser.add_argument("--vp-duration", type=float, default=None,
                        help="声纹管理：截取时长秒（--vp-compute / --vp-enroll，默认 5）")
    parser.add_argument("--vp-top", type=int, default=None, help="声纹管理：打印候选数量")
    parser.add_argument("--check-env", action="store_true",
                        help="环境自检：解释器、ffmpeg、可选依赖、模型、声纹库、云端凭据")
    parser.add_argument("--install-deps", action="store_true",
                        help="安装 requirements.txt 中的声纹可选依赖")
    args = parser.parse_args(argv)

    # 依赖安装与自检都在主流程之前短路：它们不需要媒体文件与凭据
    if args.install_deps:
        return install_deps()

    # Every entry point, including library management, resolves the same configuration.
    project_dir = Path.cwd()
    cfg = load_config(args.config, project_dir)
    args.roster = args.roster or cfg.roster_path or None
    # Sampling writes through cfg; keep the CLI override consistent with read-only routing.
    cfg.roster_path = args.roster or ""
    if args.no_voiceprint:
        cfg.vp_enabled = False
    elif args.voiceprint:
        cfg.vp_enabled = True
    if args.vp_threshold is not None:
        cfg.vp_threshold = args.vp_threshold
    if args.vp_db:
        cfg.vp_db = args.vp_db
    if args.vp_model:
        cfg.vp_model_path = args.vp_model

    # 声纹管理子命令：拼成 voiceprint 原生参数后转发，不进入转写主流程。
    # 只有当存在真正的管理动作时才转发——--vp-db / --vp-threshold / --vp-model
    # 同时也是主流程的配置覆盖项，单给它们不能把整条命令变成 --vp-list。
    forward: list = []
    if args.vp_list:
        forward.append("--list")
    for flag, value in (("--query", args.vp_query), ("--compute", args.vp_compute),
                        ("--enroll", args.vp_enroll), ("--remove", args.vp_remove)):
        if value:
            forward += [flag, value]
    if forward:
        if args.vp_confirmed_audio:
            forward.append("--confirmed-audio")
        for flag, value in (("--index", args.vp_index), ("--start", args.vp_start),
                            ("--duration", args.vp_duration), ("--top", args.vp_top),
                            ("--db", args.vp_db), ("--threshold", args.vp_threshold),
                            ("--model", args.vp_model)):
            if value is not None:
                forward += [flag, str(value)]
        return vp.main(forward, cfg=cfg)

    if args.list_speakers:
        return roster._main(["--path", args.roster] if args.roster else [])

    if args.samples:
        cfg.speaker_samples = args.samples
    if args.sample_chars:
        cfg.sample_chars = args.sample_chars

    # 自检不需要媒体文件，故置于输入校验之前；--no-voiceprint 也不影响自检（它查的是环境而非开关）
    if args.check_env:
        return check_env(cfg, project_dir)

    # 写入开关后即返回：这是纯配置操作，不需要媒体文件
    if args.vp_switch:
        value = {"on": True, "off": False, "auto": None}[args.vp_switch]
        path = save_vp_switch(value, args.config, project_dir)
        print(f"[vp] 已写入 {path}：vp_enabled = "
              f"{ {True: 'true', False: 'false', None: 'null（未设置）'}[value] }")
        return 0

    if sum(bool(v) for v in (args.inputs, args.from_json, args.resume)) != 1:
        parser.error("请选择媒体文件、--from-json 或 --resume 其中一种输入")
    if (args.clear_edits or args.undo_edit or args.show_edits or args.find) and not args.from_json:
        parser.error("纠错管理参数需要 --from-json")
    if args.dry_run and (args.from_json or args.resume):
        parser.error("--dry-run 仅用于媒体准备，不能与 --from-json/--resume 同用")
    if args.inputs and not any(Path(p).expanduser().is_file() for p in args.inputs):
        print("[error] 所有输入文件均不存在，未准备依赖或调用云端", file=sys.stderr)
        return 2

    # 只读子命令：查看已记录的调整 / 检索句子。不转写、不联网、不花钱、不写文件
    if args.show_edits or args.find:
        if not args.from_json:
            parser.error("--show-edits / --find 需要用 --from-json 指定结果 JSON")
        json_file = Path(args.from_json)
        try:
            sentences = json.loads(json_file.read_text(encoding="utf-8"))["transcripts"][0]["sentences"]
        except (OSError, ValueError, KeyError) as exc:
            print(f"[edit] 读取结果 JSON 失败：{exc}", file=sys.stderr)
            return 2
        if args.find:
            find_sentences(sentences, args.find, load_existing_map(json_file))
        if args.show_edits:
            print_edits(json_file, sentences)
        return 0

    sample_action = bool(args.samples_list or args.samples_accept or args.samples_reject)
    if args.resample and not args.continue_processing:
        parser.error('--resample 需要 --continue')
    if (args.continue_processing or args.attribute or args.replace_text or sample_action) and not args.from_json:
        parser.error('继续处理、片段审核及采样管理需要 --from-json')
    if sample_action and (args.continue_processing or args.attribute or args.replace_text or args.speaker_map or args.text or args.split or args.reassign or args.merge or args.clear_edits or args.undo_edit):
        parser.error('采样确认不能与修改/重新采样混用；先更新并试听，再确认')
    # 首次声纹在原始结果保存后执行；校准及采样不重复匹配，入库单独检查依赖。
    if args.samples_accept:
        decided = resolve_vp_switch(cfg, project_dir, allow_prompt=True)
        if decided is None:
            print_vp_pending(voiceprint_readiness(cfg, project_dir))
            return 3
        cfg.vp_enabled = decided
        if args.samples_accept and not decided:
            raise ValueError("声纹已关闭，不能确认入库；可显式 --voiceprint")

    # 前置校验：API Key 与 OSS 配置（dry-run 与 from-json 允许缺 OSS）
    missing_key = cfg.missing(["api_key"])
    if missing_key and not args.dry_run and not args.from_json:
        print(
            "[config] 缺少百炼 API Key。设置环境变量 BAILIAN_API_KEY，"
            "或在配置文件中填写 api_key（参考 config.example.json）。",
            file=sys.stderr,
        )
        return 2
    if not args.dry_run and not args.from_json:
        missing_oss = cfg.missing(["oss_endpoint", "oss_bucket", "oss_access_key_id", "oss_access_key_secret"])
        if missing_oss:
            raise ValueError("OSS 配置缺失: " + ", ".join(missing_oss))
        if cfg.oss_url_mode == "signed" and cfg.oss_url_ttl < cfg.max_wait + 300:
            raise ValueError("签名 URL 有效期至少应覆盖 max_wait + 300 秒；恢复任务时需另外留足云端排队时间")

    ffmpeg, ffprobe = ensure_tools(cfg, project_dir)
    print(f"[tools] ffmpeg={ffmpeg}\n[tools] ffprobe={ffprobe}")
    if cfg.source:
        print(f"[config] 配置来源: {cfg.source}")
    print(f"[config] 模型: {cfg.model}")

    if args.from_json:
        process_file(cfg, Path(args.from_json), ffmpeg, ffprobe, args)
        return 0
    if args.resume:
        process_file(cfg, Path(args.resume), ffmpeg, ffprobe, args)
        return 0

    successes, failures = 0, 0
    for raw in args.inputs:
        src = Path(raw).expanduser()
        if not src.is_file():
            print(f"[skip] 文件不存在: {src}", file=sys.stderr)
            failures += 1
            continue
        try:
            process_file(cfg, src, ffmpeg, ffprobe, args)
            successes += 1
        except VoiceprintPending:
            return 3
        except (OSError, ValueError, RuntimeError, KeyError, IndexError, EOFError) as exc:
            failures += 1
            print(f"[failed] {src.name}: {_safe_error(exc)}", file=sys.stderr)

    print(f"[summary] 成功 {successes}，失败 {failures}")
    return 0 if failures == 0 else 4 if successes else 2


def _safe_error(exc: Exception) -> str:
    """Avoid logging signed query strings or URL-bearing transport tracebacks."""
    import re
    text = re.sub(r"https?://[^\s'\"<>]+", "[URL 已隐藏]", str(exc))
    return text or type(exc).__name__


def main(argv=None) -> int:
    """Give batch callers stable failures without tracebacks containing private URLs."""
    try:
        return _main(argv)
    except VoiceprintPending as exc:
        print(f"[pending] {exc}", file=sys.stderr)
        return 3
    except (OSError, ValueError, RuntimeError, KeyError, IndexError, EOFError) as exc:
        print(f"[error] {_safe_error(exc)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
