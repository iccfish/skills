"""Validated local hotwords and offline CRUD; never print unrelated configuration."""
from __future__ import annotations

import json
import os
from pathlib import Path

from localio import read_json, save_document
from paths import config_path


def term(value) -> str:
    """Normalize surrounding spaces and reject empty or control-bearing terms."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("热词必须为非空字符串")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("热词不能包含换行或控制字符")
    return value.strip()


def weight(value) -> int:
    """Follow filetrans integer weights, including the documented super-hotword value."""
    if type(value) is not int or value not in (1, 2, 3, 4, 5, 50):
        raise ValueError("热词权重必须为整数 1–5，或超级热词权重 50")
    return value


def normalize(value) -> dict[str, int]:
    """Validate the complete vocabulary so the API cannot silently drop excess terms."""
    if not isinstance(value, dict):
        raise ValueError("hotwords 必须为“热词: 整数权重”的 JSON 对象")
    result = {}
    for text, score in value.items():
        text = term(text)
        if text in result:
            raise ValueError("存在去除首尾空白后重复的热词")
        result[text] = weight(score)
    if len(result) > 2000:
        raise ValueError("热词总数不能超过 2000")
    if sum(score == 50 for score in result.values()) > 50:
        raise ValueError("权重 50 的超级热词不能超过 50 个")
    return result


def manage(action: str, text=None, *, score=None, new_text=None, explicit=None) -> dict:
    """Read effective words or atomically edit only the chosen file's hotwords field."""
    explicit = explicit or os.environ.get("MEETING_TRANSCRIBE_CONFIG")
    target = Path(explicit).expanduser() if explicit else config_path()
    document = read_json(target)
    vocabulary = document.get("hotwords", {})
    # Match load_config's field-level precedence without copying default credentials.
    if explicit and "hotwords" not in document and target.resolve() != config_path().resolve():
        vocabulary = read_json(config_path()).get("hotwords", {})
    vocabulary = normalize(vocabulary)
    if action == "list":
        return {"hotwords": vocabulary, "count": len(vocabulary)}
    text = term(text)
    if action == "query":
        if text not in vocabulary:
            raise ValueError("热词不存在")
        return {"text": text, "weight": vocabulary[text]}
    if action == "add":
        if text in vocabulary:
            raise ValueError("热词已存在；修改请用 --hotword-update")
        vocabulary[text] = weight(1 if score is None else score)
    elif action == "update":
        if text not in vocabulary:
            raise ValueError("热词不存在")
        if score is None and new_text is None:
            raise ValueError("修改热词需要 --hotword-weight 或 --hotword-rename")
        replacement = term(new_text) if new_text is not None else text
        if replacement != text and replacement in vocabulary:
            raise ValueError("新热词已存在，不能覆盖")
        score = weight(score) if score is not None else vocabulary[text]
        del vocabulary[text]
        vocabulary[replacement] = score
        text = replacement
    elif action == "remove":
        if text not in vocabulary:
            raise ValueError("热词不存在")
        del vocabulary[text]
    else:
        raise ValueError("未知热词操作")
    document["hotwords"] = normalize(vocabulary)
    # Document revisions protect other configuration fields from concurrent edits.
    save_document(target, document)
    return {"action": action, "text": text, "count": len(vocabulary),
            "config": str(target), **({"weight": vocabulary[text]} if action != "remove" else {})}


def add_arguments(parser) -> None:
    """Expose one independent CRUD action plus optional weight and rename controls."""
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--hotword-list", action="store_true", help="列出本地热词和权重")
    group.add_argument("--hotword-query", metavar="热词", help="查询单个热词的权重")
    group.add_argument("--hotword-add", metavar="热词", help="添加热词，默认权重 1")
    group.add_argument("--hotword-update", metavar="热词", help="修改已有热词的权重或词语")
    group.add_argument("--hotword-remove", metavar="热词", help="删除热词")
    parser.add_argument("--hotword-weight", type=int, help="添加/修改热词的权重：1–5 或 50")
    parser.add_argument("--hotword-rename", metavar="新热词", help="与 --hotword-update 同用，保留未指定的权重")


def dispatch(args, parser) -> int | None:
    """Short-circuit offline management and reject commands that could hide another action."""
    actions = [name for name in ("list", "query", "add", "update", "remove")
               if getattr(args, "hotword_" + name) is not None
               and getattr(args, "hotword_" + name) is not False]
    if not actions:
        if args.hotword_weight is not None or args.hotword_rename is not None:
            parser.error("--hotword-weight/--hotword-rename 必须与热词添加/修改同用")
        return None
    action = actions[0]
    allowed = {"config", "hotword_" + action, "hotword_weight", "hotword_rename"}
    for name, value in vars(args).items():
        changed = bool(value) if name == "inputs" else value != parser.get_default(name)
        if name not in allowed and changed:
            parser.error("热词管理不能与转写、恢复、依赖安装或其他管理操作混用")
    if args.hotword_weight is not None and action not in ("add", "update"):
        parser.error("--hotword-weight 只能与添加/修改同用")
    if args.hotword_rename is not None and action != "update":
        parser.error("--hotword-rename 只能与 --hotword-update 同用")
    result = manage(action, getattr(args, "hotword_" + action),
                    score=args.hotword_weight, new_text=args.hotword_rename, explicit=args.config)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
