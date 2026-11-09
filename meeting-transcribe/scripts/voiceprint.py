"""可选本地 CAM++ 声纹：Kaldi 兼容特征、ONNX 推理、带模型/前处理版本的本地库。
候选相似度不代表概率，必须人工确认；旧 NumPy 前处理库需要保留并重新录入。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import hashlib
from pathlib import Path
from urllib.request import urlretrieve
from localio import read_json, save_document, file_lock
from paths import data_dir, cache_dir, legacy_read_path

try:
    import numpy as np
    import onnxruntime as ort
    import kaldi_native_fbank as knf
    AVAILABLE = True
    IMPORT_ERROR = ""
except ImportError as exc:
    # 可选依赖缺失：标记不可用即可，调用方据此跳过声纹环节
    np = None  # type: ignore[assignment]
    ort = None  # type: ignore[assignment]
    knf = None
    AVAILABLE = False
    IMPORT_ERROR = str(exc)

SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = data_dir() / "voiceprints.json"

MODEL_FILENAME = "campplus_zh_cn_common_200k.onnx"
MODEL_SHA256 = "5c8574f7930ddde1abe72dac31ee5ff7fb7fd30cc8fbf0302881c4cb32f4a4ad"
MODEL_REVISION = "e95ce6e8d1f357014dc253ca8caa47f579a65226"
# 下载源按序尝试：国内镜像优先，官方站兜底
MODEL_URLS = (
    f"https://hf-mirror.com/Alkd/campplus-zh-cn-common-200k-onnx/resolve/{MODEL_REVISION}/{MODEL_FILENAME}",
    f"https://huggingface.co/Alkd/campplus-zh-cn-common-200k-onnx/resolve/{MODEL_REVISION}/{MODEL_FILENAME}",
)
EMBED_DIM = 192
FRONTEND = "kaldi-fbank-v2"
# 与训练端 kaldi 默认一致：25ms 窗 / 10ms 跳 / 0.97 预加重
SR = 16000
FRAME_MS, HOP_MS, N_MELS, PREEMPH = 25.0, 10.0, 80, 0.97

_session = None
_session_path = ""


def available() -> tuple:
    """返回 (是否可用, 不可用原因)。缺包时给出安装命令，便于用户自行决定。"""
    if AVAILABLE:
        return True, ""
    return False, f"缺少可选依赖（{IMPORT_ERROR}）。请在当前解释器安装 requirements.txt"


def locate_model(explicit: str = "", project_dir: Path | None = None) -> Path | None:
    """只定位不下载：返回已存在的模型路径，找不到返回 None。

    顺序：显式路径 > 用户缓存 > 旧项目 .tools/models；技能包不含模型权重。
    下载与安装目录分离，跨项目共用用户缓存。
    与 ensure_model 的区别是绝不触发网络请求，供 --check-env 自检使用。
    """
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None

    base = Path(project_dir) if project_dir else Path.cwd()
    for cand in (cache_dir() / "models" / MODEL_FILENAME,
                 base / ".tools" / "models" / MODEL_FILENAME):
        if cand.is_file():
            return cand
    return None


def ensure_model(explicit: str = "", project_dir: Path | None = None) -> Path:
    """定位或下载 ONNX 模型，返回模型文件路径。

    复用定位结果，缺失时在用户缓存内锁定下载，并校验固定摘要。
    project_dir 只用于兼容旧项目缓存，不写入安装目录。
    """
    if explicit:
        p = Path(explicit).expanduser()
        if p.is_file():
            return p
        raise FileNotFoundError(f"vp_model_path 指定的模型不存在: {p}")

    found = locate_model("", project_dir)
    if found:
        return found

    target_dir = cache_dir() / "models"
    target = target_dir / MODEL_FILENAME
    target_dir.mkdir(parents=True, exist_ok=True)
    with file_lock(target):
        if target.exists():
            verify_model(target)
            return target
        return _download_model(target)


def _download_model(target: Path) -> Path:
    """Serialize first-use downloads so concurrent projects cannot share a partial file."""
    last_error = None
    for url in MODEL_URLS:
        # 先下到 .part 再原子改名：下载中断不会留下半截文件被后续当成有效模型
        tmp = target.with_name(target.name + ".part")
        try:
            print(f"[vp] 首次使用，正在下载声纹模型: {url}")
            urlretrieve(url, tmp)
            verify_model(tmp)
            tmp.replace(target)
            print(f"[vp] 模型已保存: {target}")
            return target
        except Exception as exc:  # 换下一个源，全部失败才抛
            last_error = exc
            print(f"[vp] 该源下载失败: {exc}")
            tmp.unlink(missing_ok=True)
    raise RuntimeError(f"声纹模型下载失败（已尝试 {len(MODEL_URLS)} 个源）: {last_error}")


def get_session(model_path: Path):
    """按模型路径缓存 InferenceSession，避免同一轮任务重复加载 28MB 模型。"""
    global _session, _session_path
    if _session is None or _session_path != str(model_path):
        # The default export is content-pinned; explicit replacements need their own validation.
        if model_path.name == MODEL_FILENAME:
            verify_model(model_path)
        _session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        _session_path = str(model_path)
    return _session


def verify_model(path: Path) -> None:
    """Verify the distributed export rather than trusting a successful HTTP response."""
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if digest != MODEL_SHA256:
        raise ValueError("声纹模型 SHA256 不匹配，请核对来源；未加载该文件")


def decode_pcm(path: Path, ffmpeg: str, start_sec: float = 0.0,
               duration_sec: float | None = None) -> np.ndarray:
    """用 ffmpeg 把音频解码为 16kHz 单声道 float32 PCM。

    规格与主流程转码目标一致（16kHz / 单声道），只是不落盘 MP3，省一次编码。
    -ss 放在 -i 之前用容器索引快速定位；对说话人 embedding 而言定位误差可忽略。
    """
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
    if start_sec > 0:
        cmd += ["-ss", f"{start_sec:.3f}"]
    cmd += ["-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", str(SR), "-f", "f32le"]
    cmd += ["-t", f"{duration_sec:.3f}"] if duration_sec else []
    cmd.append("-")

    out = subprocess.run(cmd, capture_output=True, check=True)
    return np.frombuffer(out.stdout, dtype="<f4")


def fbank(pcm: np.ndarray) -> np.ndarray:
    """Use Kaldi-compatible features matching upstream FBank(dither=0, mean_nor=True).

    The old numpy frontend used a different window, mel bank and DC handling;
    its embeddings must not be mixed into this frontend's voiceprint library.
    """
    if len(pcm) < int(FRAME_MS / 1000 * SR):
        return np.zeros((0, N_MELS), dtype=np.float32)
    options = knf.FbankOptions()
    options.frame_opts.samp_freq = SR
    options.frame_opts.dither = 0.0
    options.frame_opts.frame_length_ms = FRAME_MS
    options.frame_opts.frame_shift_ms = HOP_MS
    options.frame_opts.preemph_coeff = PREEMPH
    options.frame_opts.window_type = "povey"
    options.frame_opts.remove_dc_offset = True
    options.mel_opts.num_bins = N_MELS
    options.mel_opts.low_freq = 20.0
    options.mel_opts.high_freq = 0.0
    processor = knf.OnlineFbank(options)
    processor.accept_waveform(SR, np.asarray(pcm, dtype=np.float32).tolist())
    processor.input_finished()
    feat = np.stack([processor.get_frame(i) for i in range(processor.num_frames_ready)]).astype(np.float32)
    feat -= feat.mean(axis=0, keepdims=True)
    return feat


def embed_pcm(pcm: np.ndarray, model_path: Path) -> np.ndarray:
    """由 PCM 得到 L2 归一化的 192 维 embedding。"""
    sess = get_session(model_path)
    spec = sess.get_inputs()[0]
    feat = fbank(pcm)
    if feat.shape[0] == 0:
        raise ValueError("音频过短，无法提取特征（至少需要 25ms）")

    x = feat[None]
    # 输入布局可能是 [B,T,80] 或 [B,80,T]，按签名的第二个维度判断是否需要转置
    dims = [str(d) for d in spec.shape]
    if len(dims) == 3 and dims[1].isdigit() and int(dims[1]) == N_MELS:
        x = feat.T[None]

    vec = sess.run(None, {spec.name: x})[0][0]
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm else vec


def average_embedding(vecs: list) -> np.ndarray:
    """多段向量取平均后重新归一化。

    单个片段可能受噪音或短时发声状态影响，多段平均能显著稳定匹配结果。
    """
    mean = np.asarray(vecs, dtype=np.float32).mean(axis=0)
    norm = float(np.linalg.norm(mean))
    return mean / norm if norm else mean


def pick_spans(sentences: list, spk, count: int = 3, min_sec: float = 4.0,
               max_sec: float = 20.0) -> list:
    """挑选不重叠连续片段，排除已知他人重叠与长空档；上游漏分仍须人工试听。"""
    if min_sec <= 0 or max_sec < min_sec or count <= 0:
        raise ValueError('采样时长需满足 0 < min_sec <= max_sec，数量须大于零')
    runs, current = [], None
    for row in sentences:
        if row.get('speaker_id') != spk:
            current = None
            continue
        begin, end = row['begin_time'] / 1000, row['end_time'] / 1000
        if current is None or begin - current[1] > 1.0:
            current = [begin, end]
            runs.append(current)
        else:
            current[1] = max(current[1], end)
    # Exclude any time actually occupied by another cluster, including overlapping speech.
    others = [(r['begin_time'] / 1000, r['end_time'] / 1000)
              for r in sentences if r.get('speaker_id') != spk]
    clean = []
    for begin, end in runs:
        pieces = [(begin, end)]
        for left, right in others:
            pieces = [(a, b) for x, y in pieces for a, b in
                      ([(x, y)] if right <= x or left >= y else [(x, min(y, left)), (max(x, right), y)])
                      if b - a >= min_sec]
        clean.extend(pieces)
    spans = []
    for begin, end in sorted(clean, key=lambda p: -(p[1] - p[0])):
        # Split a sufficiently long continuous turn to obtain two distinct, nonoverlapping clips.
        while end - begin >= min_sec and len(spans) < count:
            remaining = count - len(spans)
            duration = min(max_sec, (end - begin) / remaining) if end - begin >= min_sec * remaining else min(max_sec, end - begin)
            spans.append((round(begin, 3), round(duration, 3)))
            begin += duration
        if len(spans) == count:
            break
    return spans


def embed_audio(path: Path, ffmpeg: str, model_path: Path,
                start_sec: float = 0.0, duration_sec: float = 5.0) -> np.ndarray:
    """直接对音频文件的指定片段取 embedding（默认从 0 秒起 5 秒）。"""
    return embed_pcm(decode_pcm(path, ffmpeg, start_sec, duration_sec), model_path)


# --------------------------------------------------------------------------
# 声纹库
# --------------------------------------------------------------------------

def _empty_db() -> dict:
    """返回空的声纹库结构。"""
    return {"schema": 2, "model": MODEL_FILENAME, "model_sha256": MODEL_SHA256,
            "frontend": FRONTEND, "dim": EMBED_DIM, "speakers": []}


def load_db(path: str | Path | None = None) -> dict:
    """Read a valid library; corrupt biometric data must never become an empty overwrite."""
    p = Path(path) if path else data_dir() / "voiceprints.json"
    data = read_json(legacy_read_path(p, "voiceprints.json"), _empty_db())
    if not isinstance(data.get("speakers"), list):
        raise ValueError(f"声纹库结构错误，原文件已保留: {p}")
    data.setdefault("schema", 1)
    data.setdefault("speakers", [])
    return data


def save_db(db: dict, path: str | Path | None = None) -> None:
    """写回声纹库，父目录不存在时自动创建。"""
    p = Path(path) if path else data_dir() / "voiceprints.json"
    save_document(p, db)


def _cosine(a, b) -> float:
    """两个向量的余弦相似度；输入均已归一化时等价于点积。"""
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na == 0 or nb == 0:
        return 0.0
    return float(a @ b / (na * nb))


def _recompute_centroid(entry: dict) -> None:
    """按该人现有样本重算质心并归一化，使新增样本后立即生效。"""
    samples = entry.get("samples") or []
    if not samples:
        entry.pop("centroid", None)
        return
    # Removal/list maintenance must work without installing inference dependencies.
    import math
    vectors = [s["vec"] for s in samples]
    if not vectors[0] or any(len(v) != len(vectors[0]) for v in vectors):
        raise ValueError("声纹样本维度不一致，原文件已保留")
    centroid = [sum(float(v[i]) for v in vectors) / len(vectors) for i in range(len(vectors[0]))]
    norm = math.sqrt(sum(x * x for x in centroid))
    entry["centroid"] = [x / norm for x in centroid] if norm else centroid


def validate_db(db: dict, model_sha256: str = MODEL_SHA256) -> None:
    """Prevent mixing embeddings across model/frontend generations."""
    if db.get("speakers") and (db.get("frontend") != FRONTEND or db.get("model_sha256") != model_sha256):
        raise ValueError("声纹库的模型或前处理版本不匹配；请保留旧库，另建 --vp-db 并从原音频重新录入")
    if not db.get("speakers"):
        db.update(frontend=FRONTEND, model_sha256=model_sha256, schema=2)


def match(db: dict, vec, threshold: float = 0.74, top: int = 5, model_sha256: str = MODEL_SHA256) -> list:
    """在声纹库中查找最可能的候选人。

    打分取「与最佳单条样本的相似度」和「与质心的相似度」的较大值：
    前者能容纳同一人声音的多种状态，后者抗单条噪声样本。
    返回 [(姓名, 相似度, 是否过阈值)]，按相似度降序。
    """
    validate_db(db, model_sha256)
    results = []
    for entry in db.get("speakers", []):
        samples = [s["vec"] for s in (entry.get("samples") or [])]
        if not samples:
            continue
        best = max(_cosine(vec, s) for s in samples)
        centroid = entry.get("centroid")
        score = max(best, _cosine(vec, centroid)) if centroid else best
        results.append((entry.get("name", "?"), float(score), score >= threshold))

    results.sort(key=lambda r: -r[1])
    return results[:top]


def enroll(db: dict, name: str, vec, source: str = "", start_sec: float = 0.0,
           duration_sec: float = 0.0, segments: int = 1, replace_source: bool = False,
           speaker=None, aliases=(), model_sha256: str = MODEL_SHA256) -> str:
    """把一条 embedding 存入指定姓名下，返回 'added' / 'updated' / 'skipped'。

    两种去重口径：

    - 默认（手工 --vp-enroll）：同一来源文件 + 同一起始时间视为同一样本，
      因此同一文件的不同片段可以分别录入，各自成为一条独立样本。
    - replace_source=True（自动入库）：**同一文件同一个人只保留一条**，先删旧样本再写新的。
      自动入库必须走这条路——人工纠正归属后，说话人的切段会变（起点、时长都变），
      按起点去重必然失效，同一份录音每跑一次就多一条「不同代」的向量，库越用越脏。

    speaker 记录该样本对应的 speaker_id；aliases 是被合并进这个人的其它 speaker_id
    （人工 --merge 之后）。两者一起用于清掉「同一文件里旧名/旧身份」的残留样本：
    改名或合并后，旧名下的那条仍是同一段声音，留着会让 match() 给出两个互相矛盾的身份。
    """
    validate_db(db, model_sha256)
    name = (name or "").strip()
    if not name:
        return "skipped"

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    entry = next((e for e in db.get("speakers", []) if e.get("name") == name), None)
    if entry is None:
        entry = {"name": name, "created": now, "samples": []}
        db.setdefault("speakers", []).append(entry)

    status = "added"
    if replace_source and source:
        # 同一文件同一人只留最新一代向量：删掉旧样本（纠正后更纯的那份才是要留的）
        kept = [s for s in entry["samples"] if s.get("source") != source]
        if len(kept) != len(entry["samples"]):
            status = "updated"
        entry["samples"] = kept
        ids = {s for s in ({speaker} | set(aliases or ())) if s is not None}
        _drop_stale_samples(db, source, ids, name, now)
    else:
        # 同一文件同一时间点已入过库则跳过，保持手工录入的可重复运行性
        for s in entry["samples"]:
            if s.get("source") == source and abs(float(s.get("start", -1)) - float(start_sec)) < 0.01:
                return "skipped"

    entry["samples"].append({
        "vec": [round(float(v), 6) for v in vec],
        "source": source,
        "start": round(float(start_sec), 3),
        "duration": round(float(duration_sec), 3),
        "segments": segments,
        "speaker": speaker,
        "at": now,
    })
    entry["updated"] = now
    _recompute_centroid(entry)
    return status


def _drop_stale_samples(db: dict, source: str, speakers: set, keep_name: str, now: str) -> int:
    """清掉「同一文件 + 同一批 speaker_id + 不同姓名」的旧样本，返回清理条数。

    只在自动入库时调用。改名或合并后，旧名下的样本仍是同一段声音，
    留着会让 match() 给出两个近乎满分且互相矛盾的身份。
    没有 speaker 字段的旧样本不动手——宁可留着也不误删。
    """
    if not source or not speakers:
        return 0

    dropped = 0
    emptied: list = []
    for entry in db.get("speakers", []):
        if entry.get("name") == keep_name:
            continue
        samples = entry.get("samples") or []
        kept = [s for s in samples
                if not (s.get("source") == source and s.get("speaker") in speakers)]
        if len(kept) == len(samples):
            continue
        dropped += len(samples) - len(kept)
        entry["samples"] = kept
        entry["updated"] = now
        _recompute_centroid(entry)
        if not kept:
            emptied.append(entry)

    # 被清空的旧身份直接移除，免得 --vp-list 里留一堆 0 样本的空壳
    if emptied:
        dead = {id(e) for e in emptied}
        db["speakers"] = [e for e in db.get("speakers", []) if id(e) not in dead]
    return dropped


def remove(db: dict, name: str, index: int | None = None) -> int:
    """删除声纹：index 为空则删除整人，否则只删该人的第 index 条样本。返回删除条数。"""
    before = sum(len(e.get("samples") or []) for e in db.get("speakers", []))
    if index is None:
        db["speakers"] = [e for e in db.get("speakers", []) if e.get("name") != name]
    else:
        for entry in db.get("speakers", []):
            if entry.get("name") == name and 0 <= index < len(entry.get("samples") or []):
                entry["samples"].pop(index)
                _recompute_centroid(entry)
                if not entry["samples"]:
                    db["speakers"] = [e for e in db["speakers"] if e.get("name") != name]
                break
    after = sum(len(e.get("samples") or []) for e in db.get("speakers", []))
    return before - after


def query(db: dict, name: str) -> dict | None:
    """按姓名精确查询声纹条目，未找到返回 None。"""
    return next((e for e in db.get("speakers", []) if e.get("name") == name), None)


def list_db(db: dict) -> list:
    """列出声纹库概览：每人姓名、样本数、最近更新时间与来源文件。"""
    rows = []
    for entry in db.get("speakers", []):
        samples = entry.get("samples") or []
        sources = sorted({s.get("source", "") for s in samples if s.get("source")})
        rows.append({
            "name": entry.get("name", "?"),
            "samples": len(samples),
            "updated": entry.get("updated") or entry.get("created", "-"),
            "sources": sources,
        })
    rows.sort(key=lambda r: (-r["samples"], r["name"]))
    return rows


# --------------------------------------------------------------------------
# 命令行入口：说话人（声纹）管理
# --------------------------------------------------------------------------

def _print_candidates(candidates: list, threshold: float) -> None:
    """打印候选及相似度，并标注是否达到阈值。"""
    print(f"\n候选（阈值 {threshold:.2f}，分数为余弦相似度，非校准概率）：")
    if not candidates:
        print("  库为空，无候选。可先 --enroll 录入。")
        return
    for name, score, hit in candidates:
        flag = "命中" if hit else "低于阈值"
        print(f"  {score:.4f}  {name:<12} [{flag}]")


def main(argv: list | None = None, cfg=None) -> int:
    """声纹管理 CLI：列出、查询、计算、录入、删除。"""
    parser = argparse.ArgumentParser(
        description="说话人声纹管理：列出 / 查询 / 计算 / 录入 / 删除",
        epilog="示例：\n"
               "  python voiceprint.py --list\n"
               "  python voiceprint.py --query 张三\n"
               "  python voiceprint.py --compute 会议.mp3 --start 12 --duration 5\n"
               "  python voiceprint.py --enroll 张三=会议.mp3 --start 12\n"
               "  python voiceprint.py --remove 张三",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--list", action="store_true", help="列出当前所有声纹")
    parser.add_argument("--query", metavar="姓名", help="查询指定姓名的声纹详情")
    parser.add_argument("--compute", metavar="音频", help="计算指定音频的声纹并打印候选")
    parser.add_argument("--confirmed-audio", action="store_true", help="用户已试听并确认所选音频片段的身份与纯净度")
    parser.add_argument("--enroll", metavar="姓名=音频", help="把指定音频的声纹录入到该姓名下")
    parser.add_argument("--remove", metavar="姓名", help="删除该姓名的声纹")
    parser.add_argument("--index", type=int, default=None,
                        help="配合 --remove 只删第 N 条样本（从 0 开始）")
    parser.add_argument("--start", type=float, default=0.0, help="起始秒（默认 0）")
    parser.add_argument("--duration", type=float, default=5.0, help="截取时长秒（默认 5）")
    parser.add_argument("--top", type=int, default=5, help="打印候选数量（默认 5）")
    parser.add_argument("--threshold", type=float, default=0.74, help="命中阈值（默认 0.74）")
    parser.add_argument("--db", default=None, help="声纹库路径（默认用户数据目录）")
    parser.add_argument("--model", default=None, help="ONNX 模型路径（默认用户缓存，缺失时下载）")
    parser.add_argument("--config", default=None, help="与转写共用本地配置")
    args = parser.parse_args(argv)

    from config import ensure_tools, load_config
    cfg = cfg or load_config(args.config, Path.cwd())
    args.db = args.db or cfg.vp_db or None
    args.model = args.model or cfg.vp_model_path or None
    if "--threshold" not in (argv if argv is not None else sys.argv[1:]):
        args.threshold = cfg.vp_threshold
    if args.enroll and not args.confirmed_audio:
        print("手工录入前须试听并确认音频，再传 --confirmed-audio；会议采样使用 --samples-accept", file=sys.stderr)
        return 2
    db = load_db(args.db)

    # 纯库操作不需要模型与依赖
    if args.list:
        rows = list_db(db)
        if not rows:
            print("声纹库为空")
            return 0
        print(f"声纹库（{len(rows)} 人）:")
        for r in rows:
            src = "、".join(Path(s).name for s in r["sources"][:2]) or "-"
            print(f"  {r['name']:<12} {r['samples']} 条样本   最近 {r['updated']}   来源 {src}")
        return 0

    if args.query:
        entry = query(db, args.query)
        if not entry:
            print(f"未找到声纹: {args.query}")
            return 1
        print(f"姓名: {entry['name']}   创建: {entry.get('created', '-')}   "
              f"更新: {entry.get('updated', '-')}   样本 {len(entry.get('samples') or [])} 条")
        for i, s in enumerate(entry.get("samples") or []):
            print(f"  [{i}] {s.get('at', '-')}  {s.get('source', '') or '-'}  "
                  f"{s.get('start', 0)}s +{s.get('duration', 0)}s")
        return 0

    if args.remove:
        n = remove(db, args.remove, args.index)
        save_db(db, args.db)
        print(f"已删除 {n} 条声纹样本: {args.remove}"
              + (f"（第 {args.index} 条）" if args.index is not None else "（整人）"))
        return 0

    # 以下动作需要模型与可选依赖
    ok, reason = available()
    if not ok:
        print(f"[vp] {reason}", file=sys.stderr)
        return 2

    if not (args.compute or args.enroll):
        parser.print_help()
        return 0

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from config import ensure_tools, load_config  # 局部导入，避免循环依赖

    ffmpeg, _ = ensure_tools(cfg, Path.cwd())
    model_path = ensure_model(args.model or getattr(cfg, "vp_model_path", ""), Path.cwd())

    if args.compute:
        path = Path(args.compute).expanduser()
        fingerprint = hashlib.sha256(model_path.read_bytes()).hexdigest()
        vec = embed_audio(path, ffmpeg, model_path, args.start, args.duration)
        print(f"[vp] {path.name}  {args.start}s +{args.duration}s  ->  {len(vec)} 维向量")
        _print_candidates(match(db, vec, args.threshold, args.top, fingerprint), args.threshold)
        return 0

    if args.enroll:
        if "=" not in args.enroll:
            print("--enroll 格式应为 姓名=音频路径", file=sys.stderr)
            return 2
        name, _, raw = args.enroll.partition("=")
        path = Path(raw).expanduser()
        fingerprint = hashlib.sha256(model_path.read_bytes()).hexdigest()
        vec = embed_audio(path, ffmpeg, model_path, args.start, args.duration)
        status = enroll(db, name.strip(), vec, str(path.resolve()), args.start, args.duration, model_sha256=fingerprint)
        save_db(db, args.db)
        label = {"added": "已录入", "updated": "已覆盖同源样本", "skipped": "已存在（去重跳过）"}[status]
        print(f"{label}: {name.strip()} <- {path.name} {args.start}s +{args.duration}s")
        _print_candidates(match(db, vec, args.threshold, args.top, fingerprint), args.threshold)
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
