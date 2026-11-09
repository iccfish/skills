"""配置加载及 ffmpeg/ffprobe 发现。显式配置字段优先，凭据和缓存独立于安装目录。"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import zipfile
import platform
from dataclasses import dataclass, field
from pathlib import Path
from urllib.request import urlretrieve
from localio import read_json, save_document, file_lock
from paths import config_path, data_dir, cache_dir

# Installation holds source/templates; mutable configuration uses the user directory.
SKILL_ROOT = Path(__file__).resolve().parent.parent

# 内置默认值，任何来源缺失时兜底
DEFAULTS = {
    "model": "qwen-audio-3.1-asr-flash-filetrans",
    "dashscope_base": "https://dashscope.aliyuncs.com",
    "language_hints": ["zh", "en"],
    "poll_interval": 5.0,
    "max_wait": 3600.0,
    # 样例以字数为准（默认取每个说话人开头 200 字），句数仅作安全兜底，避免长句失控
    "speaker_samples": 30,
    "sample_chars": 200,
    "ffmpeg_dir": "",
    # 声纹识别（可选能力）三态开关：None=未设置（启动时按环境判定或询问用户），
    # True=启用，False=关闭。关闭后完全不加载模型，行为与接入前一致。
    "vp_enabled": None,
    "vp_model_path": "",          # 缓存模型兜底，缺失时下载到用户缓存
    "vp_threshold": 0.74,         # 暂定候选阈值；新前处理和不同场景均需校准
    "vp_segments": 3,             # 每个说话人抽取几段用于匹配
    "vp_min_seconds": 4.0,        # 单段最短时长，太短的 embedding 不可靠
    "vp_segment_seconds": 20.0,   # 单段最长截取时长，避免长段噪声
    "vp_auto_enroll": False,      # 仅兼容旧配置，自动入库已停用
    "vp_db": "",                  # 为空则使用用户数据目录
    "oss_url_mode": "public",
    "oss_url_ttl": 86400,
}

# 环境变量 -> 配置字段的映射，支持同义名（左侧优先命中）
ENV_MAP = {
    "api_key": ("BAILIAN_API_KEY", "DASHSCOPE_API_KEY"),
    "model": ("BAILIAN_ASR_MODEL",),
    "oss_endpoint": ("OSS_ENDPOINT",),
    "oss_access_key_id": ("OSS_ACCESS_KEY_ID",),
    "oss_access_key_secret": ("OSS_ACCESS_KEY_SECRET",),
    "oss_bucket": ("OSS_BUCKET",),
    "oss_host": ("OSS_HOST",),
    "ffmpeg_dir": ("FFMPEG_DIR",),
    "oss_security_token": ("OSS_SECURITY_TOKEN",),
    "oss_url_mode": ("OSS_URL_MODE",),
}

# 各平台的 ffmpeg 静态构建下载地址（GPL 版，含 ffmpeg 与 ffprobe）
FFMPEG_PACKAGES = {
    "win32": "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
}
# Windows 包内 ffmpeg/ffprobe 在 bin/ 子目录，macOS 包内直接是根目录
FFMPEG_INNER_BIN = {"win32": "bin", "darwin": ""}


@dataclass
class Config:
    """运行所需的全部配置项。"""

    api_key: str = field(default="", repr=False)
    model: str = DEFAULTS["model"]
    dashscope_base: str = DEFAULTS["dashscope_base"]
    oss_endpoint: str = ""
    oss_access_key_id: str = field(default="", repr=False)
    oss_access_key_secret: str = field(default="", repr=False)
    oss_bucket: str = ""
    oss_host: str = ""
    language_hints: list = field(default_factory=lambda: list(DEFAULTS["language_hints"]))
    poll_interval: float = DEFAULTS["poll_interval"]
    max_wait: float = DEFAULTS["max_wait"]
    speaker_samples: int = DEFAULTS["speaker_samples"]
    sample_chars: int = DEFAULTS["sample_chars"]
    ffmpeg_dir: str = ""
    vp_enabled: bool | None = DEFAULTS["vp_enabled"]
    vp_model_path: str = ""
    vp_threshold: float = DEFAULTS["vp_threshold"]
    vp_segments: int = DEFAULTS["vp_segments"]
    vp_min_seconds: float = DEFAULTS["vp_min_seconds"]
    vp_segment_seconds: float = DEFAULTS["vp_segment_seconds"]
    vp_auto_enroll: bool = False  # 兼容旧配置字段；流程不再使用自动入库
    vp_db: str = ""
    roster_path: str = ""
    source: str = ""
    oss_url_mode: str = "public"
    oss_url_ttl: int = 86400
    oss_security_token: str = field(default="", repr=False)

    def missing(self, keys: list) -> list:
        """返回指定字段中为空的项，用于前置校验时给出明确提示。"""
        return [k for k in keys if not getattr(self, k)]


def _tri_state(value) -> bool | None:
    """解析三态布尔开关，返回 True / False / None（未设置）。

    空字符串、null、缺失都视为「未设置」，交由上层根据环境自动判定或询问用户；
    显式写 false/0/no/off 才算关闭。注意不能直接用 bool()，否则空串会变成 False，
    把「没配」误判成「用户明确关掉了」。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if not text:
        return None
    return text in ("1", "true", "yes", "on")


def _read_json(path: Path) -> dict:
    """Only an absent auto-discovered configuration may be empty."""
    data = read_json(path)
    for name in ("ffmpeg_dir", "vp_model_path", "vp_db", "roster_path"):
        if data.get(name):
            target = Path(data[name]).expanduser()
            data[name] = str(target.resolve() if target.is_absolute() else (path.resolve().parent / target).resolve())
    return data


def load_config(explicit: str | None = None, project_dir: Path | None = None) -> Config:
    """Explicit file fields > environment > discovered file > built-in defaults."""
    file_cfg: dict = {}
    explicit_cfg: dict = {}
    loaded_from = ""

    explicit = explicit or os.environ.get("MEETING_TRANSCRIBE_CONFIG")
    existing = [config_path()] if config_path().is_file() else []
    if not existing and not explicit:
        legacy = [SKILL_ROOT / "config.json", Path.home() / ".workbuddy" / "meeting-transcribe.json"]
        if any(p.is_file() for p in legacy):
            raise RuntimeError(f"发现旧配置；请运行 scripts/migrate.py --apply 迁移到 {config_path()}")
    for candidate in existing:
        file_cfg.update(_read_json(candidate))
        loaded_from = str(candidate)
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"指定配置文件不存在: {p}")
        explicit_cfg = _read_json(p)
        loaded_from = str(p)

    data = dict(DEFAULTS)
    data.update({k: v for k, v in file_cfg.items() if k not in ("_comment", "_说明")})

    # Environment overrides discovered files; the explicit file is applied last.
    for field_name, env_names in ENV_MAP.items():
        for env in env_names:
            val = os.environ.get(env, "").strip()
            if val:
                data[field_name] = val
                break
    data.update({k: v for k, v in explicit_cfg.items() if not k.startswith("_")})

    cfg = Config(
        api_key=data.get("api_key", ""),
        model=data.get("model", DEFAULTS["model"]),
        dashscope_base=data.get("dashscope_base", DEFAULTS["dashscope_base"]),
        oss_endpoint=data.get("oss_endpoint", ""),
        oss_access_key_id=data.get("oss_access_key_id", ""),
        oss_access_key_secret=data.get("oss_access_key_secret", ""),
        oss_bucket=data.get("oss_bucket", ""),
        oss_host=data.get("oss_host", ""),
        language_hints=data.get("language_hints") or list(DEFAULTS["language_hints"]),
        poll_interval=float(data.get("poll_interval", DEFAULTS["poll_interval"])),
        max_wait=float(data.get("max_wait", DEFAULTS["max_wait"])),
        speaker_samples=int(data.get("speaker_samples", DEFAULTS["speaker_samples"])),
        sample_chars=int(data.get("sample_chars", DEFAULTS["sample_chars"])),
        ffmpeg_dir=data.get("ffmpeg_dir", ""),
        # 三态开关：空值表示未设置，交给启动时的环境判定/用户选择决定
        vp_enabled=_tri_state(data.get("vp_enabled", DEFAULTS["vp_enabled"])),
        vp_model_path=data.get("vp_model_path", ""),
        vp_threshold=float(data.get("vp_threshold", DEFAULTS["vp_threshold"])),
        vp_segments=int(data.get("vp_segments", DEFAULTS["vp_segments"])),
        vp_min_seconds=float(data.get("vp_min_seconds", DEFAULTS["vp_min_seconds"])),
        vp_segment_seconds=float(data.get("vp_segment_seconds", DEFAULTS["vp_segment_seconds"])),
        vp_auto_enroll=False,  # 旧配置即使为 true 也不能绕过试听确认
        vp_db=data.get("vp_db") or str(data_dir() / "voiceprints.json"),
        roster_path=data.get("roster_path") or str(data_dir() / "speakers.json"),
        source=loaded_from,
        oss_url_mode=data.get("oss_url_mode", "public"),
        oss_url_ttl=int(data.get("oss_url_ttl", 86400)),
        oss_security_token=data.get("oss_security_token", ""),
    )
    if cfg.poll_interval <= 0 or cfg.max_wait <= 0:
        raise ValueError("poll_interval 和 max_wait 必须大于零")
    if cfg.vp_segments <= 0 or not 0 < cfg.vp_min_seconds <= cfg.vp_segment_seconds:
        raise ValueError('声纹片段数须大于零，时长需满足 0 < vp_min_seconds <= vp_segment_seconds')
    if cfg.oss_url_mode not in ("public", "signed"):
        raise ValueError("oss_url_mode 必须为 public 或 signed")
    if not 60 <= cfg.oss_url_ttl <= 604800:
        raise ValueError("oss_url_ttl 必须在 60 到 604800 秒之间")
    return cfg


def save_vp_switch(value, explicit: str | None = None,
                   project_dir: Path | None = None) -> Path:
    """把声纹开关写回配置文件，返回被修改的文件路径。

    默认只写用户配置；--config 或 MEETING_TRANSCRIBE_CONFIG 可显式覆盖。
    value 传 None 表示恢复「未设置」（写成 null）。
    """
    explicit = explicit or os.environ.get("MEETING_TRANSCRIBE_CONFIG")
    target = Path(explicit).expanduser() if explicit else config_path()

    data = read_json(target)
    data["vp_enabled"] = value
    save_document(target, data)
    return target


def _in_dir(directory: Path, name: str) -> str | None:
    """在指定目录查找可执行文件，Windows 下自动补 .exe。"""
    exe_suffix = ".exe" if sys.platform == "win32" else ""
    for cand in (directory / (name + exe_suffix), directory / name):
        if cand.is_file():
            return str(cand)
    return None


def _download_ffmpeg(project_dir: Path) -> Path:
    """Download a supported complete toolchain into the per-user cache."""
    plat = "win32" if sys.platform == "win32" else "darwin" if sys.platform == "darwin" else "linux"
    url = FFMPEG_PACKAGES.get(plat)
    if not url or (plat == "win32" and platform.machine().lower() not in ("amd64", "x86_64")):
        raise RuntimeError(
            f"平台 {plat}/{platform.machine()} 无已适配下载源，请安装 ffmpeg 和 ffprobe，并用 FFMPEG_DIR 指定目录。"
        )

    target = cache_dir() / "ffmpeg"
    target.mkdir(parents=True, exist_ok=True)
    zip_path = target / "ffmpeg-package.zip"

    print(f"[tools] 未找到完整工具链，正在下载到用户缓存: {url}")
    with file_lock(target / "download"):
        try:
            urlretrieve(url, zip_path)
            inner = FFMPEG_INNER_BIN.get(plat, "")
            with zipfile.ZipFile(zip_path) as zf:
                members = {os.path.basename(m): m for m in zf.namelist()
                           if not inner or f"/{inner}/" in "/" + m.lstrip("./")}
                required = ("ffmpeg.exe", "ffprobe.exe") if plat == "win32" else ("ffmpeg", "ffprobe")
                if any(name not in members for name in required):
                    raise RuntimeError("下载包必须同时包含 ffmpeg 与 ffprobe，请手动安装完整工具链")
                for name in required:
                    dest = target / name
                    temporary = target / (name + ".part")
                    try:
                        with zf.open(members[name]) as source, temporary.open("wb") as dst:
                            shutil.copyfileobj(source, dst)
                        temporary.chmod(temporary.stat().st_mode | stat.S_IEXEC)
                        temporary.replace(dest)
                    finally:
                        temporary.unlink(missing_ok=True)
        finally:
            zip_path.unlink(missing_ok=True)
    return target


def find_tools(cfg: Config, project_dir: Path) -> tuple:
    """Discover both executables without any download or environment mutation."""
    # 优先：配置或环境变量指定的目录
    if cfg.ffmpeg_dir:
        ff = _in_dir(Path(cfg.ffmpeg_dir), "ffmpeg")
        fp = _in_dir(Path(cfg.ffmpeg_dir), "ffprobe")
        if ff and fp:
            return ff, fp

    # Shared cache is writable even when the installed skill is read-only.
    cached = cache_dir() / "ffmpeg"
    ff, fp = _in_dir(cached, "ffmpeg"), _in_dir(cached, "ffprobe")
    if ff and fp:
        return ff, fp

    # 兼容旧项目内缓存。
    local = project_dir / ".tools" / "ffmpeg"
    ff = _in_dir(local, "ffmpeg")
    fp = _in_dir(local, "ffprobe")
    if ff and fp:
        return ff, fp

    # 最后：系统 PATH
    ff = shutil.which("ffmpeg")
    fp = shutil.which("ffprobe")
    if ff and fp:
        return ff, fp

    return None, None


def ensure_tools(cfg: Config, project_dir: Path) -> tuple:
    """Require a complete discovered or supported downloadable toolchain."""
    ff, fp = find_tools(cfg, project_dir)
    if ff and fp:
        return ff, fp
    bin_dir = _download_ffmpeg(project_dir)
    ff = _in_dir(bin_dir, "ffmpeg")
    fp = _in_dir(bin_dir, "ffprobe")
    if not ff or not fp:
        raise RuntimeError("下载后缺少 ffmpeg 或 ffprobe，请手动安装完整工具链。")
    return ff, fp
