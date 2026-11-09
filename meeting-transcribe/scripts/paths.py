"""Keep credentials, personal data and caches outside the installed skill."""
from __future__ import annotations

import os
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent


def config_path() -> Path:
    """Follow the repository's agent-skills configuration convention."""
    base = os.environ.get("AGENT_SKILLS_CONFIG_DIR")
    if not base:
        base = str(Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "agent-skills")
    return Path(base).expanduser() / "meeting-transcribe.json"


def data_dir() -> Path:
    """Resolve a per-user mutable data directory on every supported platform."""
    explicit = os.environ.get("MEETING_TRANSCRIBE_DATA_DIR")
    if explicit:
        return Path(explicit).expanduser()
    return config_path().parent / "meeting-transcribe"


def cache_dir() -> Path:
    """Resolve disposable model/tool caches independently of personal data."""
    explicit = os.environ.get("MEETING_TRANSCRIBE_CACHE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    base = os.environ.get("XDG_CACHE_HOME")
    if not base:
        base = str(Path(os.environ["LOCALAPPDATA"]) / "Cache") if sys.platform == "win32" and os.environ.get("LOCALAPPDATA") else str(Path.home() / ".cache")
    return Path(base) / "agent-skills" / "meeting-transcribe"


def legacy_read_path(target: Path, name: str) -> Path:
    """Require an explicit migration before using legacy personal data."""
    target = Path(target)
    legacy = SKILL_ROOT / name
    if target.resolve() == (data_dir() / name).resolve() and not target.exists() and legacy.is_file():
        raise RuntimeError(f"发现旧数据 {legacy}；请运行 scripts/migrate.py --apply 迁移到 {target.parent}")
    return target
