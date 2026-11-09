"""
media.py —— 媒体探测与音频准备。

职责：判断输入是视频还是音频、是否满足“直接用”的条件，必要时转码为
16 kHz / 64 kbps / 单声道 MP3，并生成不覆盖任何现有文件的输出路径。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

# 转码目标规格：ASR 场景的标准配置，体积小、兼容性好
TARGET_ARGS = ["-map", "0:a:0", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "64k"]
# “可直接使用”的上限：码率 128k、采样率 44100 Hz、单声道
MAX_BITRATE = 128_000
MAX_SAMPLE_RATE = 44100


def probe(ffprobe: str, path: Path) -> dict:
    """用 ffprobe 读取媒体信息。

    返回字典包含：has_video / has_audio / codec / sample_rate / channels / bit_rate / duration。
    """
    cmd = [
        ffprobe, "-v", "error",
        "-show_entries", "stream=index,codec_type,codec_name,sample_rate,channels,bit_rate",
        "-show_entries", "format=duration,bit_rate",
        "-of", "json", str(path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe 读取失败: {out.stderr.strip() or path}")

    data = json.loads(out.stdout or "{}")
    streams = data.get("streams", [])
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    video = next((s for s in streams if s.get("codec_type") == "video"), None)

    def to_int(v) -> int:
        """把 ffprobe 的字符串/空值安全转成整数。"""
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0

    fmt = data.get("format", {})
    bit_rate = to_int(audio.get("bit_rate")) or to_int(fmt.get("bit_rate"))
    return {
        "has_video": video is not None,
        "has_audio": bool(audio),
        "codec": audio.get("codec_name", ""),
        "sample_rate": to_int(audio.get("sample_rate")),
        "channels": to_int(audio.get("channels")),
        "bit_rate": bit_rate,
        "duration": float(fmt.get("duration") or 0),
    }


def needs_transcode(info: dict) -> tuple:
    """判断是否需要转码。返回 (是否需要, 原因)。"""
    if not info["has_audio"]:
        raise RuntimeError("媒体文件没有音轨，无法转写。")
    if info["has_video"]:
        return True, "输入为视频，需抽取音轨"
    if info["codec"] != "mp3":
        return True, f"非 MP3 音频（{info['codec'] or '未知编码'}）"
    if info["channels"] > 1:
        return True, f"{info['channels']} 声道，需转单声道"
    if info["sample_rate"] > MAX_SAMPLE_RATE:
        return True, f"采样率 {info['sample_rate']} Hz 高于 {MAX_SAMPLE_RATE} Hz"
    if info["bit_rate"] > MAX_BITRATE:
        return True, f"码率 {info['bit_rate'] // 1000} kb/s 高于 128 kb/s"
    return False, "MP3 且满足 单声道 ≤128k ≤44100Hz，直接使用"


# 本技能自己产出的后缀。用 --from-json 传入 xxx.asr.json 时，
# 需要先剥掉这层后缀再拼新后缀，否则会得到 xxx.asr.asr.json 这种双扩展名垃圾文件。
PRODUCT_SUFFIXES = (".asr.json",)


def strip_product_suffix(src: Path) -> Path:
    """剥掉本技能自身的产物后缀，还原到源主文件名。

    例：会议.asr.json -> 会议；会议.m4a -> 会议.m4a（非产物后缀不动）。
    """
    name = src.name
    for suffix in PRODUCT_SUFFIXES:
        if name.lower().endswith(suffix.lower()):
            return src.with_name(name[: -len(suffix)])
    return src


def build_output_path(src: Path, ext: str, overwrite: bool = False) -> Path:
    """生成与源主文件名一致的输出路径。

    overwrite=True 时直接返回主名路径（用于 --from-json 原地重生成 TXT/JSON，
    即改名字后覆盖旧产物，而不是堆出 -1、-2 副本）。

    否则两种情况追加序号：目标已存在，或目标就是源文件本身（避免转码覆盖原始文件）。
    """
    is_product = src.name.lower().endswith(".asr.json")
    base = strip_product_suffix(src)
    # base 已剥掉产物后缀；再去掉源自身的扩展名，得到真正的主名
    stem = base.name if is_product else base.stem if base.suffix else base.name

    cand = base.with_name(stem + ext)
    if overwrite:
        return cand
    if not cand.exists() and cand.resolve() != src.resolve():
        return cand
    n = 1
    while True:
        alt = base.with_name(f"{stem}-{n}{ext}")
        if not alt.exists():
            return alt
        n += 1


def output_group(src: Path, *, existing: bool = False) -> dict:
    """Choose one collision suffix for all artifacts, preserving dotted ASR stems."""
    src = Path(src).resolve()
    product = src.name.lower().endswith(".asr.json")
    stem = src.name[:-len(".asr.json")] if product else src.stem
    extensions = {"json": ".asr.json", "meta": ".meta.json", "txt": ".txt", "task": ".task.json"}
    n = 0
    while True:
        name = stem if n == 0 else f"{stem}-{n}"
        group = {key: src.with_name(name + ext) for key, ext in extensions.items()}
        stage_paths = [src.with_name(name + suffix) for suffix in
                       ('-原始转写稿.txt', '-人工审核稿.txt', '-说话人分离.txt', '.samples.json',
                        '-归属转写稿未确认版.txt', '-归属校准版.txt', '.attribution.json',
                        '-会议纪要.html', '.report.json', '.minutes.json')]
        if existing or not any(p.exists() for p in [*group.values(), *stage_paths]):
            if existing and product:
                group["json"] = src
            return group
        n += 1


def transcode(ffmpeg: str, src: Path, dst: Path) -> Path:
    """把任意媒体转码为 16 kHz / 64k / 单声道 MP3。"""
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", str(src)] + TARGET_ARGS + [str(dst)]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if out.returncode != 0 or not dst.is_file():
        raise RuntimeError(f"ffmpeg 转码失败: {out.stderr.strip()[-500:]}")
    return dst


def prepare_audio(ffmpeg: str, ffprobe: str, src: Path, keep_temp: bool = False) -> tuple:
    """完成音频准备，返回 (最终音频路径, 是否为转码产物)。"""
    info = probe(ffprobe, src)
    need, reason = needs_transcode(info)
    print(f"[media] {src.name}: {reason}")

    if not need:
        return src, False

    dst = build_output_path(src, ".mp3")
    transcode(ffmpeg, src, dst)
    size_mb = dst.stat().st_size / 1024 / 1024
    print(f"[media] 已转码 -> {dst.name}（{size_mb:.2f} MiB，16kHz/64k/单声道）")
    return dst, True
