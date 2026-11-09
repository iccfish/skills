"""说话人历史名单：只记录确认姓名，跨会议累积；损坏保留、原子保存并拒绝并发覆盖。"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from localio import read_json, save_document
from paths import data_dir, legacy_read_path

SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = data_dir() / "speakers.json"


def _empty() -> dict:
    """返回空的名单结构。"""
    return {"names": [], "assignments": []}


def load(path: str | Path | None = None) -> dict:
    """Read legacy data compatibly, but never replace damaged personal information."""
    p = Path(path) if path else data_dir() / "speakers.json"
    data = read_json(legacy_read_path(p, "speakers.json"), _empty())
    if not isinstance(data.get("names"), list) or not isinstance(data.get("assignments"), list):
        raise ValueError(f"名单结构错误，原文件已保留: {p}")
    data.setdefault("names", [])
    data.setdefault("assignments", [])
    return data


def save(roster: dict, path: str | Path | None = None) -> None:
    """写回名单文件，父目录不存在时自动创建。"""
    p = Path(path) if path else data_dir() / "speakers.json"
    save_document(p, roster)


def known_names(roster: dict) -> list:
    """返回已知姓名列表，按使用次数降序、最近使用时间降序排列。"""
    entries = [e for e in roster.get("names", []) if isinstance(e, dict) and e.get("name")]
    entries.sort(key=lambda e: str(e.get("last_used", "")), reverse=True)
    entries.sort(key=lambda e: -int(e.get("count", 0)))
    return [e["name"] for e in entries]


def record(roster: dict, name: str, file_label: str = "", speaker=None) -> bool:
    """记录一次命名：新名字返回 True，已存在的累加计数并返回 False。"""
    name = (name or "").strip()
    if not name:
        return False

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    if file_label and any(a.get("file") == file_label and a.get("speaker") == speaker
                          and a.get("name") == name for a in roster.get("assignments", [])):
        return False
    is_new = True
    for entry in roster.get("names", []):
        if entry.get("name") == name:
            entry["count"] = int(entry.get("count", 0)) + 1
            entry["last_used"] = now
            files = entry.setdefault("files", [])
            if file_label and file_label not in files:
                files.append(file_label)
            is_new = False
            break
    if is_new:
        roster.setdefault("names", []).append({
            "name": name,
            "count": 1,
            "last_used": now,
            "files": [file_label] if file_label else [],
        })

    roster.setdefault("assignments", []).append({
        "at": now,
        "file": file_label,
        "speaker": speaker,
        "name": name,
    })
    return is_new


def _main(argv: list) -> int:
    """命令行入口：查看名单（--list）、手工增删（--add / --remove）。"""
    path = None
    for i, a in enumerate(argv):
        if a == "--path" and i + 1 < len(argv):
            path = argv[i + 1]
    if path is None:
        from config import load_config
        explicit = argv[argv.index("--config") + 1] if "--config" in argv else None
        path = load_config(explicit, Path.cwd()).roster_path or None

    if "--add" in argv:
        idx = argv.index("--add")
        value = argv[idx + 1] if idx + 1 < len(argv) else ""
        roster = load(path)
        new = record(roster, value)
        save(roster, path)
        print(f"{'新增' if new else '已存在，计数+1'}: {value}")
        return 0

    if "--remove" in argv:
        idx = argv.index("--remove")
        value = argv[idx + 1] if idx + 1 < len(argv) else ""
        roster = load(path)
        before = len(roster.get("names", []))
        roster["names"] = [e for e in roster.get("names", []) if e.get("name") != value]
        save(roster, path)
        print(f"已删除 {before - len(roster['names'])} 条: {value}")
        return 0

    # 默认：列出名单
    roster = load(path)
    names = known_names(roster)
    if not names:
        print("名单为空")
        return 0
    for e in sorted(roster.get("names", []), key=lambda x: -int(x.get("count", 0))):
        print(f"  {e['name']}  (使用 {e.get('count', 0)} 次, 最近 {e.get('last_used', '-')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
