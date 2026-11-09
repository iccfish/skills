"""Strict JSON loading, atomic writes and fail-fast local writer coordination."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


class Document(dict):
    """Carry the read revision so a later save cannot silently lose another writer."""

    def __init__(self, data, path, revision):
        super().__init__(data)
        self.path, self.revision = Path(path).resolve(), revision


def revision(path: Path) -> str | None:
    """Hash an existing file; only a genuinely absent file has no revision."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def read_json(path: Path, default=None) -> Document:
    """Never treat a corrupt/unreadable document as an empty replacement."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return Document(default if default is not None else {}, path, None)
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f"JSON 损坏，原文件已保留，请修复后重试: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON 顶层必须是对象，原文件已保留: {path}")
    return Document(value, path, hashlib.sha256(raw).hexdigest())


@contextmanager
def file_lock(path: Path):
    """Use an exclusive sidecar lock; never guess whether another writer is alive."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError(f"文件正在被处理；若进程已退出，请核实后移除残留锁: {lock}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        lock.unlink(missing_ok=True)


def atomic_text(path: Path, text: str, *, overwrite: bool = True) -> None:
    """Flush a same-directory temporary file before publishing the complete content."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            # A hard link publishes without overwriting a file created concurrently.
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, value: dict, *, overwrite: bool = True) -> None:
    """Serialize JSON completely before any existing file is replaced."""
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n", overwrite=overwrite)


def save_document(path: Path, value: dict) -> None:
    """Reject stale writes or implicit migration of legacy private data."""
    path = Path(path).resolve()
    with file_lock(path):
        if isinstance(value, Document):
            if value.path != path:
                raise RuntimeError(f"旧数据仅兼容读取；请先手动复制 {value.path} 到 {path}，或显式指定旧路径")
            if revision(path) != value.revision:
                raise RuntimeError(f"文件已被其他操作修改，请重新读取后重试: {path}")
        atomic_json(path, value)
        if isinstance(value, Document):
            value.revision = revision(path)
