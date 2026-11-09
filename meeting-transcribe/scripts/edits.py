"""
edits.py —— 人工调整记录：解析、归一化、回放。

背景：自动说话人分离会出现错误归属（他人插话被并进长段、跨信道被拆成两个人）。
用户会手工纠正——改名、整位合并、逐句改判、句中摘段、修正文本。这些纠正必须可追溯、
可重放：写在 .meta.json 的 edits.adjustments 里，--from-json 重新生成时自动重放，
不需要用户再纠一遍。

设计要点：
1. 记录是 append-only 的原始操作，回放顺序即执行顺序，因此结果可复现；
2. 归一化视图（summary）把「整位迁移」收敛成一条 merge、其余保留为逐句 moves，
   避免反复试错把等价操作堆成长串；
3. 定位基准是**原始句序号**（apply 时打上 _oi 标记），而不是当前列表下标：
   split 会让一句变成多段、后续下标整体后移，用下标定位必然错位；
   (原始序号, 原始起始毫秒) 双校验，对不上或指向多处的一律跳过并**保留原记录**；
4. 拆句（split）用 words 的字级时间戳切分，切出的段各带自己的 begin/end，
   这样声纹切段就不会把两个人的声音混进同一个向量里。
"""

from __future__ import annotations

import json
import re
import sys
import time
import hashlib
from pathlib import Path
from localio import read_json

# 记录结构版本：字段语义变化时递增，供后续兼容判断
# v2：新增 split（句中摘段）操作，定位改用原始句序号 _oi
EDIT_VERSION = 4

# HH:MM:SS / MM:SS
_TS_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{1,2})(?:\.(\d{1,3}))?$")
# 纯秒数（可带小数）
_SEC_RE = re.compile(r"^\d+(?:\.\d+)?$")


def now() -> str:
    """当前时间，作为记录的时间戳（精确到秒，便于人工核对先后）。"""
    return time.strftime("%Y-%m-%d %H:%M:%S")


def fmt_ts(ms) -> str:
    """毫秒 -> HH:MM:SS。"""
    total = int(ms or 0) // 1000
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def parse_ts(text) -> int | None:
    """把 HH:MM:SS / MM:SS / 纯秒数解析成毫秒；解析不了返回 None。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    m = _TS_RE.match(raw)
    if m:
        hh = int(m.group(1) or 0)
        mm, ss = int(m.group(2)), int(m.group(3))
        frac = int((m.group(4) or "0").ljust(3, "0"))
        return ((hh * 3600 + mm * 60 + ss) * 1000) + frac
    if _SEC_RE.match(raw):
        return int(float(raw) * 1000)
    return None


def parse_speaker_map(text: str) -> dict:
    """解析 "0=张三,1=李四" 形式的映射，返回 {speaker_id: 姓名}。"""
    result = {}
    for part in (text or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.rsplit("=", 1)
        try:
            result[int(k.strip())] = v.strip()
        except ValueError:
            continue
    return result


def _split_pair(spec: str) -> tuple:
    """把 "左=右" 拆成两段；以最后一个等号分割，容忍左侧含等号的文本。"""
    if not spec or "=" not in spec:
        return None, None
    left, right = spec.rsplit("=", 1)
    return left.strip(), right.strip()


def _to_int(text):
    """安全的整数转换，失败返回 None。"""
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None


def resolve_speaker(text, names: dict | None = None):
    """把目标说话人解析成 speaker_id：优先按整数，其次按姓名反查已知映射。

    用户在对话里说「这段是李四说的」，没必要让他先查 speaker_id；
    智能体据此可以直接把原话翻成命令。查不到返回 None，由调用方报错。
    """
    sid = _to_int(text)
    if sid is not None:
        return sid
    key = str(text or "").strip()
    if names and key in names:
        return names[key]
    return None


def parse_reassign(spec: str, sentences: list, names: dict | None = None) -> dict | None:
    """解析改判规格，返回一条记录或 None（解析失败时打印原因）。

    两种写法：
        "12,13=1"                按句序号（从 0 起）
        "00:12:30-00:12:55=1"    按时间区间，含端点，按句子起始时间判定
    目标可以用 speaker_id，也可以直接写姓名（需已有映射）。
    """
    left, right = _split_pair(spec)
    if left is None:
        print(f"[edit] 无法解析 --reassign {spec!r}（应为 12,13=1 或 00:12:30-00:12:55=1）",
              file=sys.stderr)
        return None

    target = resolve_speaker(right, names)
    if target is None:
        print(f"[edit] --reassign {spec!r} 的目标既不是整数、也不在已知姓名里：{right!r}"
              f"（已知姓名：{sorted((names or {}))}）", file=sys.stderr)
        return None

    # 带冒号又有连字符 -> 时间区间；否则按句序号列表
    if "-" in left:
        head, tail = left.split("-", 1)
        start, end = parse_ts(head), parse_ts(tail)
        if start is None or end is None:
            print(f"[edit] --reassign {spec!r} 的时间区间无法解析：{left!r}", file=sys.stderr)
            return None
        if end < start:
            start, end = end, start
        return {"op": "reassign_range", "start_ms": start, "end_ms": end,
                "to": target, "at": now()}

    items = []
    for part in left.split(","):
        idx = _to_int(part)
        if idx is None or not 0 <= idx < len(sentences):
            print(f"[edit] 句序号越界，已忽略：{part!r}（共 {len(sentences)} 句，序号从 0 起）",
                  file=sys.stderr)
            continue
        items.append({"idx": idx, "begin_ms": sentences[idx].get("begin_time", 0)})
    if not items:
        return None
    return {"op": "reassign", "items": items, "to": target, "at": now()}


def tag_origins(sentences: list) -> list:
    """给每句打上「原始序号」标记，作为整份调整记录的稳定定位锚。

    `_oi` 原始句序号（0 起，一份 ASR 结果内唯一）、`_part` 句内拆分序号（未拆为 0）、
    `_obegin` 该原始句的起始毫秒（用于二次校验：换了录音就不该再命中）。

    为什么需要：split 会把一句换成多段，列表下标整体后移；若记录用当前下标定位，
    第二条记录就会改到别人的句子上。原始序号不随拆分变化，才能反复重放。
    标记只在内存里存在，写 .asr.json 发生在打标之前，不会污染原始结果。
    """
    for i, s in enumerate(sentences):
        s["_oi"] = i
        s["_part"] = 0
        s["_obegin"] = int(s.get("begin_time") or 0)
    return sentences


def snapshot(sentences: list) -> dict:
    """取调整前的快照：{(原始序号, 拆分序号): (speaker_id, text)}。

    用 key 而非列表下标对齐，拆句后仍能逐条比对「这句原来是谁说的、原来写的什么」。
    """
    return {(s.get("_oi", i), s.get("_part", 0)): (s.get("speaker_id"), s.get("text", ""))
            for i, s in enumerate(sentences)}


def _linear_times(text: str, begin_ms: int, end_ms: int) -> list:
    """没有 words 时的兜底：按字符数把 [begin, end] 均分给每个字符。"""
    n = len(text) or 1
    step = (end_ms - begin_ms) / n
    return [(int(begin_ms + i * step), int(begin_ms + (i + 1) * step)) for i in range(len(text))]


def _fill_gaps(times: list, begin_ms: int, end_ms: int) -> list:
    """把对不齐的字符时间（None）用前后已知值线性补齐，保证每个字符都有时间。"""
    n = len(times)
    i = 0
    while i < n:
        if times[i] is not None:
            i += 1
            continue
        start = i
        while i < n and times[i] is None:
            i += 1
        left = times[start - 1][1] if start > 0 else begin_ms
        right = times[i][0] if i < n else end_ms
        span = i - start
        step = (right - left) / (span + 1) if right > left else 0
        for k in range(span):
            b = int(left + step * (k + 1))
            times[start + k] = (b, int(left + step * (k + 2)) if right > left else b)
    return times


def char_times(sentence: dict) -> list:
    """按 words 推出「逐字符时间表」，长度与 sentence['text'] 一致。

    返回 [(begin_ms, end_ms), ...]。标点没有独立音频时长，压在所属词的末尾；
    words 缺失或与 text 对不齐时退化为按字符数均分，宁可粗一点也不能没有时间。
    """
    text = sentence.get("text") or ""
    if not text:
        return []

    begin = int(sentence.get("begin_time") or 0)
    end = int(sentence.get("end_time") or begin)
    words = sentence.get("words") or []
    if not words:
        return _linear_times(text, begin, end)

    # 把每个词（含标点）展开成逐字符序列，作为对齐用的参照流
    stream: list = []
    for w in words:
        b = int(w.get("begin_time") or begin)
        e = int(w.get("end_time") or b)
        for ch in (w.get("text") or ""):
            stream.append((ch, b, e))
        for ch in (w.get("punctuation") or ""):
            stream.append((ch, e, e))

    # 双指针顺序匹配：text 里每个字符在流里找下一个同字，跳过识别漏字等错位
    out: list = [None] * len(text)
    j = 0
    for i, ch in enumerate(text):
        k = j
        while k < len(stream) and stream[k][0] != ch:
            k += 1
        if k < len(stream):
            out[i] = (stream[k][1], stream[k][2])
            j = k + 1
    return _fill_gaps(out, begin, end)


def _find_insensitive(text: str, needle: str, start: int = 0) -> int:
    """在 text 中查找 needle（去首尾空白后），返回起始下标；找不到返回 -1。"""
    probe = (needle or "").strip()
    if not probe:
        return start if 0 <= start <= len(text) else -1
    return text.find(probe, max(0, start))


def plan_carve(sentence: dict, carve: dict) -> tuple:
    """把「摘出一段」翻译成 [(文本, speaker), ...] 的分段方案。

    carve 形如 {"from": "也就是说", "to": "这件事", "speaker": 1}：from / to 都可省略，
    省略 from 表示从句首开始，省略 to 表示一直到句尾（即常见的「从某处切开」）。
    返回 (方案, 错误说明)；方案里相邻且同 speaker 的段会被合并，不做无意义的拆分。
    """
    text = sentence.get("text") or ""
    orig = sentence.get("speaker_id")
    target = carve.get("speaker")
    if not text or target is None:
        return None, "该句为空或缺少目标 speaker"

    head = carve.get("from") or ""
    tail = carve.get("to") or ""
    begin = _find_insensitive(text, head, 0) if head else 0
    if begin < 0:
        return None, f"句内找不到起点文本 {head!r}"
    if tail:
        at = _find_insensitive(text, tail, begin)
        if at < 0:
            return None, f"句内找不到终点文本 {tail!r}（需在起点之后）"
        end = at + len(tail.strip())
    else:
        end = len(text)
    if end <= begin:
        return None, "起点与终点重合，无法切分"

    segs = []
    if begin > 0:
        segs.append((text[:begin], orig))
    segs.append((text[begin:end], target))
    if end < len(text):
        segs.append((text[end:], orig))
    return _coalesce(segs), ""


def _coalesce(segs: list) -> list:
    """合并相邻且归属相同的段，避免把一句切成「同一个人 / 同一个人」这种废拆分。"""
    out: list = []
    for piece, spk in segs:
        if not piece:
            continue
        if out and out[-1][1] == spk:
            out[-1] = (out[-1][0] + piece, spk)
        else:
            out.append((piece, spk))
    return out


def parse_split(spec: str, sentences: list, names: dict | None = None) -> dict | None:
    """解析 --split，返回一条 split 记录；解析失败打印原因并返回 None。

    写法：`定位=起点..终点=speaker`，`..` 任一侧可省略。
        12=也就是说..这件事=1            按原始句序号定位
        00:10:25=也就是说..这件事=1      按起始时间定位（落在该句时段内即可）
        12=也就是说=1                    只给起点：从该处切到句尾，后半归 1
    目标 speaker 也可以直接写姓名（需已有映射）。
    """
    # 定位段不含等号，故用首个等号切定位；目标 speaker 取最后一个等号右侧，
    # 中间的「起点..终点」原样保留（正文里可能带等号）
    if "=" not in spec:
        print(f"[edit] 无法解析 --split {spec!r}（应为 12=起点..终点=1）", file=sys.stderr)
        return None
    loc, rest = spec.split("=", 1)
    loc = loc.strip()
    span, spk_text = _split_pair(rest)
    if span is None:
        print(f"[edit] --split {spec!r} 缺少目标 speaker（应为 12=起点..终点=1）", file=sys.stderr)
        return None
    target = resolve_speaker(spk_text, names)
    if target is None:
        print(f"[edit] --split {spec!r} 的目标既不是整数、也不在已知姓名里：{spk_text!r}"
              f"（已知姓名：{sorted((names or {}))}）", file=sys.stderr)
        return None

    idx = _to_int(loc)
    begin_ms = None
    if idx is None:
        ts = parse_ts(loc)
        if ts is None:
            print(f"[edit] --split {spec!r} 的定位既不是句序号也不是时间：{loc!r}", file=sys.stderr)
            return None
        # 用户说的是「10:25 那句话」，落在该句时段内即可，不要求毫秒完全相等
        hits = [i for i, s in enumerate(sentences) if s.get("begin_time") == ts]
        if not hits:
            hits = [i for i, s in enumerate(sentences)
                    if int(s.get("begin_time") or 0) <= ts <= int(s.get("end_time") or 0)]
        if len(hits) != 1:
            near = [(i, fmt_ts(sentences[i].get("begin_time"))) for i in hits[:5]]
            print(f"[edit] --split {spec!r}：{fmt_ts(ts)} 匹配到 {len(hits)} 句"
                  f"{'（' + str(near) + '）' if near else ''}，请改用句序号定位", file=sys.stderr)
            return None
        idx, begin_ms = hits[0], None
    if not 0 <= idx < len(sentences):
        print(f"[edit] 句序号越界，已忽略：{idx}（共 {len(sentences)} 句，序号从 0 起）",
              file=sys.stderr)
        return None

    if ".." in span:
        head, tail = span.split("..", 1)
    else:
        head, tail = span, ""
    return {
        "op": "split",
        "idx": idx,
        "begin_ms": begin_ms if begin_ms is not None else sentences[idx].get("begin_time", 0),
        "carve": {"from": head.strip(), "to": tail.strip(), "speaker": target},
        "at": now(),
    }


def parse_text_fix(spec: str, sentences: list) -> dict | None:
    """解析 "5=修正后的文本"，并把原文一并存进记录（便于回溯改了什么）。"""
    left, right = _split_pair(spec)
    if left is None:
        print(f"[edit] 无法解析 --text {spec!r}（应为 5=修正后的文本）", file=sys.stderr)
        return None
    idx = _to_int(left)
    if idx is None or not 0 <= idx < len(sentences):
        print(f"[edit] 句序号越界，已忽略：{left!r}（共 {len(sentences)} 句）", file=sys.stderr)
        return None
    return {"op": "text", "idx": idx, "begin_ms": sentences[idx].get("begin_time", 0),
            "old": sentences[idx].get("text", ""), "text": right, "at": now()}


def from_cli(sentences: list, renames="", reassign=(), merges=(), texts=(), splits=(),
             names: dict | None = None) -> list:
    """把命令行参数转成调整记录列表；坏条目打印原因后跳过，不中断流程。

    names 是「姓名 → speaker_id」的反查表，让 --reassign/--split 可以直接写姓名。
    """
    records = []

    for spk, name in parse_speaker_map(renames).items():
        if name:
            records.append({"op": "rename", "speaker": spk, "name": name, "at": now()})

    for spec in reassign or ():
        rec = parse_reassign(spec, sentences, names)
        if rec:
            records.append(rec)

    for spec in splits or ():
        rec = parse_split(spec, sentences, names)
        if rec:
            records.append(rec)

    for spec in merges or ():
        left, right = _split_pair(spec)
        src, dst = _to_int(left), _to_int(right)
        if src is None or dst is None:
            print(f"[edit] 无法解析 --merge {spec!r}（应为 3=1）", file=sys.stderr)
            continue
        records.append({"op": "merge", "from": src, "to": dst, "at": now()})

    for spec in texts or ():
        rec = parse_text_fix(spec, sentences)
        if rec:
            records.append(rec)

    return records


def review_records(sentences: list, attributes=(), replacements=()) -> list:
    """Anchor fragment names and literal corrections without renaming a whole ASR cluster."""
    records = []
    for spec in attributes:
        if spec.count('=') < 2:
            raise ValueError('--attribute 格式：句序号=起点..终点=姓名')
        loc, rest = spec.split('=', 1)
        span, name = rest.rsplit('=', 1)
        if not name.strip():
            raise ValueError('片段姓名不能为空')
        # Stable negative IDs distinguish manually identified fragments from noisy ASR clusters.
        sid = -1 - int(hashlib.sha256(name.strip().encode('utf-8')).hexdigest()[:12], 16)
        rec = parse_split(f'{loc}={span}={sid}', sentences)
        if rec is None:
            raise ValueError('人工片段定位失败，请先 --find 核对原话与句序号')
        rec.update(op='attribute', name=name.strip())
        records.append(rec)
    for spec in replacements:
        loc, sep, rest = spec.partition('=')
        old, arrow, new = rest.partition('=>')
        idx = _to_int(loc)
        if not sep or not arrow or not old or idx is None or not 0 <= idx < len(sentences):
            raise ValueError('--replace-text 格式：原始句序号=原文=>新文；原文不得为空')
        records.append({'op': 'replace_text', 'idx': idx,
                        'begin_ms': sentences[idx]['begin_time'], 'old': old, 'new': new, 'at': now()})
    return records


def load(meta_path) -> list:
    """Missing metadata is empty; damaged edit history must never be discarded."""
    data = read_json(Path(meta_path))
    adjustments = (data.get("edits") or {}).get("adjustments")
    if adjustments is None:
        return []
    if not isinstance(adjustments, list) or any(not isinstance(r, dict) for r in adjustments):
        raise ValueError(f"调整日志格式错误，原文件保留: {meta_path}")
    return ensure_ids(adjustments)


def ensure_ids(records: list) -> list:
    """Assign deterministic IDs to legacy operations without changing their meaning."""
    out = []
    for i, rec in enumerate(records):
        item = dict(rec)
        if not item.get("id"):
            raw = json.dumps({k: v for k, v in item.items() if k not in ("at", "id")}, sort_keys=True, ensure_ascii=False)
            item["id"] = hashlib.sha256(f"{i}:{raw}".encode()).hexdigest()[:16]
        out.append(item)
    if len({r['id'] for r in out}) != len(out):
        raise ValueError("调整日志含重复 id，请先核对原始记录")
    return out


def active_records(records: list) -> list:
    """Undo deactivates a prior operation; every replay starts from the original ASR."""
    disabled = set()
    known = {}
    for rec in records:
        if rec.get("op") == "undo":
            target = known.get(rec.get("target"))
            if not target or target.get("op") == "undo":
                raise ValueError("撤销记录未指向前面的有效操作")
            disabled.add(rec["target"])
        known[rec["id"]] = rec
    return [r for r in records if r.get("op") != "undo" and r["id"] not in disabled]


def append_undo(records: list, targets: list) -> list:
    """Accept a displayed operation ID or its one-based list number."""
    records = ensure_ids(records)
    additions = []
    for target in targets:
        value = str(target)
        rec = next((r for r in records if r["id"] == value), None)
        if rec is None and value.isdigit() and 1 <= int(value) <= len(records):
            rec = records[int(value) - 1]
        if rec is None or rec.get("op") == "undo":
            raise ValueError(f"无法撤销该记录: {value}；先用 --show-edits 查看编号")
        additions.append({"op": "undo", "target": rec["id"], "at": now()})
    return dedupe(records, additions)


def dedupe(existing: list, new: list) -> list:
    """追加新记录时按「除时间戳外完全一致」去重。

    同一条改判被反复执行（例如智能体重跑两次）不会堆成两份，
    但时间戳不同、内容不同的记录全部保留——那是真实的人工操作轨迹。
    """
    def key(rec: dict) -> str:
        return json.dumps({k: v for k, v in rec.items() if k not in ("at", "id")},
                          sort_keys=True, ensure_ascii=False)

    existing = ensure_ids(existing)
    seen = {key(r) for r in active_records(existing)}
    seen.update(key(r) for r in existing if r.get("op") == "undo")
    out = list(existing)
    for rec in new:
        k = key(rec)
        if k in seen:
            continue
        seen.add(k)
        out.append(rec)
    return ensure_ids(out)


class _UnionFind:
    """并查集：merge 记录把人折叠到同一 id，后续记录须按折叠后的 id 生效。"""

    def __init__(self):
        self.parent: dict = {}

    def find(self, x):
        """找根（带路径压缩），保证整条链后续查询接近 O(1)。"""
        if x is None:
            return None
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, src, dst) -> bool:
        """把 src 折进 dst；已是同一人时返回 False（供调用方判定为重复记录）。"""
        a, b = self.find(src), self.find(dst)
        if a == b:
            return False
        self.parent[a] = b
        return True


def _resolve_hits(sentences: list, idx, begin_ms) -> list:
    """把 (原始句序号, 原始起始毫秒) 解析成当前列表中的下标列表。

    返回列表而非单个下标：split 会让一个原始句对应多个当前条目，改判/改文本都要覆盖全部段。
    二次校验用 _obegin 而不是当前 begin_time——拆开后的段起点已变，用当前值校验必然失配。
    录音被重新识别过时原始序号与起始时间全变，此时按起始时间唯一匹配兜底，仍不唯一就返回空
    （交给调用方记 skipped，宁可不动也不改错人）。
    """
    if not isinstance(idx, int) or idx < 0:
        return []
    hits = [i for i, s in enumerate(sentences) if s.get("_oi") == idx]
    if hits and (begin_ms is None
                 or all(sentences[i].get("_obegin") == begin_ms for i in hits)):
        return hits
    cands = [i for i, s in enumerate(sentences) if s.get("_obegin") == begin_ms]
    return cands if len(cands) == 1 else []


def _plan_parts(sentence: dict, parts: list) -> tuple:
    """按显式 parts 在句内顺序定位各段文本，拼成 [(文本, speaker), ...]。

    给智能体直接构造记录用（它能精确看到原文，比 CLI 的起点/终点写法更直接）。
    段间若有未覆盖的字符（标点差异等），归入前一段，避免丢字。
    """
    text = sentence.get("text") or ""
    orig = sentence.get("speaker_id")
    segs: list = []
    cursor = 0
    cur_spk = orig
    for part in parts:
        piece = part.get("text") or ""
        spk = part.get("speaker")
        if spk is None:
            return None, "parts 里有条目缺少 speaker"
        at = text.find(piece, cursor) if piece else cursor
        if at < 0:
            return None, f"句内找不到片段 {piece[:12]!r}（需按出现顺序给出）"
        if at > cursor:
            segs.append((text[cursor:at], cur_spk))
        segs.append((piece, spk))
        cursor, cur_spk = at + len(piece), spk
    if cursor < len(text):
        segs.append((text[cursor:], cur_spk))
    return _coalesce(segs), ""


def _build_split(orig: dict, plan: list) -> list:
    """按分段方案把一句换成多句：文本、时间、words 都按字符区间切开。

    时间轴处理：首段起点沿用原句起点、末段终点沿用原句终点，中间各段取段首字符的时间，
    并强制单调不减——保证切出来的段首尾相接、不重叠、不越界，声纹切段才不会抽到跨越两人的音频。
    """
    text = orig.get("text") or ""
    times = char_times(orig)
    begin = int(orig.get("begin_time") or 0)
    end = int(orig.get("end_time") or begin)

    spans, cursor = [], 0
    for piece, spk in plan:
        at = text.find(piece, cursor)
        if at < 0:          # plan 由原文本切片而来，理论上不会发生，兜底防丢字
            at = cursor
        spans.append((at, at + len(piece), piece, spk))
        cursor = at + len(piece)

    starts: list = []
    for k, (s, e, _piece, _spk) in enumerate(spans):
        if k == 0:
            starts.append(begin)
            continue
        found = next((times[i][0] for i in range(s, min(e, len(times)))), None)
        starts.append(max(found if found is not None else starts[-1], starts[-1]))
    ends = starts[1:] + [end]

    words = orig.get("words") or []
    out: list = []
    for k, ((_s, _e, piece, spk), b, en) in enumerate(zip(spans, starts, ends)):
        last = k == len(spans) - 1
        out.append({
            "begin_time": b,
            "end_time": en,
            "text": piece,
            "sentence_id": orig.get("sentence_id"),
            "speaker_id": spk,
            "words": [w for w in words
                      if int(w.get("begin_time") or 0) >= b
                      and (last or int(w.get("begin_time") or 0) < en)],
            "_oi": orig.get("_oi", 0),
            "_part": k,
            "_obegin": orig.get("_obegin", begin),
            **({'_manual_name': orig['_manual_name']} if orig.get('_manual_name') and spk == orig.get('speaker_id') else {}),
        })
    return out


# 段首标点在此集合内时前移给上一段，避免出现「，然后说…」这种以标点开头的发言
_LEAD_PUNCT = "，。、！？；：,.!?;:…"


def _shift_punct(segs: list) -> list:
    """把每段开头的标点归还给前一段，让拆分边界落在语义停顿处而非标点之后。

    只搬运字符、不改顺序，因此「各段拼接仍等于原句」这一点仍然成立；
    整段都是标点的极端情况跳过，避免把该段削成空串。
    """
    out = [[piece, spk] for piece, spk in segs]
    for i in range(1, len(out)):
        piece = out[i][0]
        k = 0
        while k < len(piece) and piece[k] in _LEAD_PUNCT:
            k += 1
        if k and k < len(piece):
            out[i - 1][0] += piece[:k]
            out[i][0] = piece[k:]
    return [(a, b) for a, b in out]


def apply(sentences: list, adjustments: list) -> dict:
    """按记录顺序就地修改 sentences（speaker_id / text / 拆分），返回执行结果。

    返回 {"names": {最终id: 姓名}, "applied": [...], "skipped": [...],
          "conflicts": [...], "alias": {原始id: 最终id}}。
    任何一条记录出问题都只进 skipped/conflicts，绝不抛异常——人工调整不能让转写白跑。
    """
    if sentences and "_oi" not in sentences[0]:
        tag_origins(sentences)

    valid = {s.get("speaker_id") for s in sentences}
    uf = _UnionFind()
    applied: list = []
    skipped: list = []
    warnings: list = []
    rename_records: list = []

    def range_hits(start: int, end: int) -> list:
        """区间改判：任一拆分段落点落在区间内即视为该原始句命中，整组一起改。

        拆开后的后续段时间上仍在原句范围内，只按 begin_time 判定会漏掉它们。
        """
        groups: dict = {}
        for i, s in enumerate(sentences):
            groups.setdefault(s.get("_oi"), []).append(i)
        hits: list = []
        for members in groups.values():
            if any(start <= (sentences[i].get("begin_time") or 0) <= end for i in members):
                hits.extend(members)
        return hits


    for rec in active_records(ensure_ids(adjustments)):
        op = rec.get("op")

        if op == "merge":
            src, dst = rec.get("from"), rec.get("to")
            if src not in valid or dst not in valid:
                skipped.append({"record": rec,
                                "reason": f"说话人不存在：{src}->{dst}（现有 {sorted(valid)}）"})
                continue
            if not uf.union(src, dst):
                skipped.append({"record": rec, "reason": "两者已是同一说话人（重复合并）"})
                continue
            target = uf.find(dst)
            # 把已折叠到该根的句子统一改成根 id，后续记录与统计才有一致的 id
            for s in sentences:
                if s.get("speaker_id") != target and uf.find(s.get("speaker_id")) == target:
                    s["speaker_id"] = target
            applied.append(rec)

        elif op == "rename":
            # 姓名在回放结束后统一按折叠后的 id 归位，这里只收集
            rename_records.append(rec)

        elif op in ("reassign", "reassign_range"):
            target = _to_int(rec.get("to"))
            if target not in valid:
                skipped.append({"record": rec,
                                "reason": f"目标 speaker {rec.get('to')} 不存在（现有 {sorted(valid)}）"})
                continue
            target = uf.find(target)
            if op == "reassign":
                hits = []
                for item in rec.get("items") or []:
                    for i in _resolve_hits(sentences, item.get("idx"), item.get("begin_ms")):
                        if i not in hits:
                            hits.append(i)
                if not hits:
                    skipped.append({"record": rec, "reason": "句序号与起始时间对不上（结果已变化）"})
                    continue
                total = len(rec.get("items") or [])
                if len(hits) < total:
                    # 部分句定位失败：能对上的照常改，剩下的留痕提示，不整条丢弃
                    warnings.append({"record": rec,
                                     "reason": f"{total - len(hits)}/{total} 句定位失败（结果已变化），"
                                               f"已应用其余 {len(hits)} 句"})
            else:
                start, end = rec.get("start_ms") or 0, rec.get("end_ms") or 0
                hits = range_hits(start, end)
                if not hits:
                    skipped.append({"record": rec,
                                    "reason": f"{fmt_ts(start)}~{fmt_ts(end)} 区间内没有句子"})
                    continue
            for i in hits:
                sentences[i]["speaker_id"] = target
            applied.append({**rec, "resolved": hits})

        elif op in ("split", "attribute"):
            hits = _resolve_hits(sentences, rec.get("idx"), rec.get("begin_ms"))
            if not hits:
                skipped.append({"record": rec, "reason": "句序号与起始时间对不上（结果已变化）"})
                continue
            if op == 'attribute' and len(hits) > 1:
                candidates = [i for i in hits if plan_carve(sentences[i], rec.get('carve') or {})[0]]
                if len(candidates) == 1:
                    hits = candidates
            if len(hits) > 1:
                skipped.append({"record": rec,
                                "reason": f"该句已拆成 {len(hits)} 段，跳过以免重复切分"})
                continue
            orig = sentences[hits[0]]
            if rec.get("parts"):
                plan, err = _plan_parts(orig, rec["parts"])
            else:
                plan, err = plan_carve(orig, rec.get("carve") or {})
            if not plan:
                skipped.append({"record": rec, "reason": err or "无法定位切分点"})
                continue
            if len(plan) < 2 and op != 'attribute':
                skipped.append({"record": rec, "reason": "切分后仍是一段（起终点归属相同）"})
                continue
            pieces = _build_split(orig, _shift_punct(plan))
            if op == 'attribute':
                target = rec['carve']['speaker']
                valid.add(target)
                rename_records.append({'speaker': target, 'name': rec['name']})
                for piece in pieces:
                    if piece['speaker_id'] == target:
                        piece['_manual_name'] = rec['name']
            sentences[hits[0]:hits[0] + 1] = pieces
            applied.append({**rec, "resolved": hits})

        elif op == 'replace_text':
            hits = _resolve_hits(sentences, rec.get('idx'), rec.get('begin_ms'))
            candidates = [(i, sentences[i]['text'].count(rec['old'])) for i in hits]
            if sum(n for _, n in candidates) != 1:
                skipped.append({'record': rec, 'reason': '替换原文未唯一命中，未猜测或覆盖'})
                continue
            i = next(i for i, n in candidates if n)
            row = sentences[i]
            at = row['text'].index(rec['old'])
            end = at + len(rec['old'])
            times = char_times(row)
            replacement = rec['new']
            new_times = _linear_times(replacement, times[at][0], times[end - 1][1])
            text = row['text'][:at] + replacement + row['text'][end:]
            all_times = times[:at] + new_times + times[end:]
            row['text'] = text
            row['words'] = [{'text': c, 'begin_time': b, 'end_time': e} for c, (b, e) in zip(text, all_times)]
            applied.append({**rec, 'resolved': [i]})

        elif op == "text":
            hits = _resolve_hits(sentences, rec.get("idx"), rec.get("begin_ms"))
            if not hits:
                skipped.append({"record": rec, "reason": "句序号与起始时间对不上（结果已变化）"})
                continue
            # 改文本是「整句重写」：该句若已拆成多段，分段边界随之失效，
            # 因此把各段的时间与 words 并回首段、删掉其余段，避免留下空文本的碎片段
            head = sentences[hits[0]]
            head["text"] = rec.get("text", "")
            if len(hits) > 1:
                head["end_time"] = max(int(sentences[i].get("end_time") or 0) for i in hits)
                head["words"] = [w for i in hits for w in (sentences[i].get("words") or [])]
                for i in sorted(hits[1:], reverse=True):
                    del sentences[i]
                warnings.append({"record": rec,
                                 "reason": f"该句原有 {len(hits)} 段，整句改写后已合并回一段"})
            applied.append({**rec, "resolved": hits})

        else:
            skipped.append({"record": rec, "reason": f"未知操作类型：{op}"})

    # 姓名归位：按记录顺序解析到折叠后的 id，后写的覆盖先写的（并记一条冲突留痕）
    names: dict = {}
    conflicts: list = []
    for rec in rename_records:
        spk = rec.get("speaker")
        name = rec.get("name")
        if not isinstance(spk, int) or not name:
            skipped.append({"record": rec, "reason": "记录缺少 speaker 或 name"})
            continue
        if spk not in valid:
            skipped.append({"record": rec, "reason": f"说话人 {spk} 不存在（现有 {sorted(valid)}）"})
            continue
        canon = uf.find(spk)
        if canon in names and names[canon] != name:
            conflicts.append({"record": rec,
                              "reason": f"speaker {canon} 先已命名为「{names[canon]}」，以本条为准"})
        names[canon] = name

    alias = {k: uf.find(k) for k in list(uf.parent)}
    return {"names": names, "applied": applied, "skipped": skipped,
            "warnings": warnings, "conflicts": conflicts, "alias": alias}


def summarize(before: dict, sentences: list, result: dict, total: int = 0) -> dict:
    """把「原始归属 → 当前归属」的净效果归纳成可读视图。

    before 是 snapshot() 取的 {(原始序号, 拆分序号): (speaker_id, text)}。
    整位迁移（某个原始说话人的所有句子都迁到同一个新 id）收敛成一条 merge，
    剩余逐句变化保留为 moves —— 这样反复试错不会在视图里堆成一长串等价记录。

    拆句单独统计：同一原始序号下有多个段即为拆过。
    拆分但不改归属（把一句话切成两半、谁说的都没变）不该混进 moves，
    因此按原始序号整体判断净效果，而不是逐段比对。
    """
    orig_of: dict = {}
    for (oi, _part), (spk, _text) in before.items():
        orig_of.setdefault(oi, spk)

    current: dict = {}
    for i, s in enumerate(sentences):
        current.setdefault(s.get("_oi", i), []).append(s)

    # 整位迁移按「原始说话人」判断：他名下的所有句子（含拆出来的段）是否都去了同一个 id。
    # 不能按单句判断——只改一句时那组当然只有一个归属，会误报成整位合并。
    by_spk: dict = {}
    for oi, spk in orig_of.items():
        by_spk.setdefault(spk, []).append(oi)

    merges, covered = [], set()
    for spk, ois in by_spk.items():
        rows_all = [r for oi in ois for r in (current.get(oi) or [])]
        targets = {r.get("speaker_id") for r in rows_all}
        if rows_all and len(targets) == 1:
            target = next(iter(targets))
            if target != spk:
                merges.append({"from": spk, "to": target, "sentences": len(rows_all)})
                covered.update(ois)

    moves, texts = [], []
    for (oi, part), (spk, text) in before.items():
        rows = current.get(oi) or []
        row = next((r for r in rows if r.get("_part") == part), None)
        if row is None:
            continue

        # 拆过的句子不按段比对：段的归属变化属于「摘段」的语义，已由 splits 表达；
        # 文本也要按整句拼接后比，否则「原文 → 拆成三段」会被误算成一次文本修正。
        if len(rows) > 1:
            joined = "".join(r.get("text", "") for r in rows)
            if joined != text:
                texts.append({"idx": oi, "part": part, "ts": fmt_ts(row.get("begin_time")),
                              "old": text, "new": joined})
            continue

        if oi not in covered and row.get("speaker_id") != spk:
            moves.append({"idx": oi, "part": part, "ts": fmt_ts(row.get("begin_time")),
                          "from": spk, "to": row.get("speaker_id")})
        if row.get("text", "") != text:
            texts.append({"idx": oi, "part": part, "ts": fmt_ts(row.get("begin_time")),
                          "old": text, "new": row.get("text", "")})

    splits = [
        {"idx": oi, "parts": len(rows),
         "ts": fmt_ts(min((r.get("begin_time") or 0) for r in rows)),
         "speakers": sorted({r.get("speaker_id") for r in rows}, key=lambda x: (x is None, x))}
        for oi, rows in sorted(current.items()) if len(rows) > 1
    ]

    return {
        "version": EDIT_VERSION,
        "count": total or len(result.get("applied") or []),
        "renames": {str(k): v for k, v in sorted(result.get("names", {}).items())},
        "merges": merges,
        "moves": moves,
        "texts": texts,
        "splits": splits,
        "applied": len(result.get("applied") or []),
        "skipped": result.get("skipped") or [],
        "warnings": result.get("warnings") or [],
        "conflicts": result.get("conflicts") or [],
    }


def describe(summary: dict) -> str:
    """一行人类可读的调整摘要（只统计归属/文本/拆分纠正），供 TXT 头部与日志复用。

    刻意不含「命名」：命名是每次流程的常规动作，写进 TXT 头部只会变成噪音；
    姓名的变化在 meta.edits.adjustments 里仍逐条留痕。
    """
    parts = []
    if summary.get("splits"):
        parts.append(f"句中摘段 {len(summary['splits'])} 处")
    if summary.get("merges"):
        parts.append(f"整位合并 {len(summary['merges'])} 处")
    if summary.get("moves"):
        parts.append(f"逐句改判 {len(summary['moves'])} 句")
    if summary.get("texts"):
        parts.append(f"文本修正 {len(summary['texts'])} 句")
    if summary.get("skipped"):
        parts.append(f"失效记录 {len(summary['skipped'])} 条")
    return "、".join(parts)


def describe_record(rec: dict) -> str:
    """把一条原始调整记录渲染成人话，供 --show-edits 与排障日志使用。"""
    op, at = rec.get("op"), rec.get("at", "")
    if op == "undo":
        return f"[{at}] 撤销记录 {rec.get('target')}（从原始结果重放）"
    if op == "rename":
        return f"[{at}] 命名 speaker {rec.get('speaker')} → {rec.get('name')}"
    if op == 'attribute':
        return f"[{at}] 句 {rec.get('idx')} 的片段「{rec.get('carve', {}).get('from', '')}..{rec.get('carve', {}).get('to', '')}」指定为 {rec.get('name')}"
    if op == 'replace_text':
        return f"[{at}] 句 {rec.get('idx')} 逐字修正：{rec.get('old')!r} → {rec.get('new')!r}"
    if op == "merge":
        return f"[{at}] 整位合并 speaker {rec.get('from')} → speaker {rec.get('to')}"
    if op == "reassign":
        idxs = [it.get("idx") for it in rec.get("items") or []]
        return f"[{at}] 改判句 {idxs} → speaker {rec.get('to')}"
    if op == "reassign_range":
        return (f"[{at}] 改判 {fmt_ts(rec.get('start_ms'))}~{fmt_ts(rec.get('end_ms'))}"
                f" → speaker {rec.get('to')}")
    if op == "split":
        carve = rec.get("carve") or {}
        if carve:
            span = f"{carve.get('from') or '（句首）'}..{carve.get('to') or '（句尾）'}"
            return f"[{at}] 句中摘段：{rec.get('idx')} 句「{span}」→ speaker {carve.get('speaker')}"
        segs = "、".join(f"{str(p.get('text'))[:10]}→{p.get('speaker')}"
                         for p in rec.get("parts") or [])
        return f"[{at}] 拆句：{rec.get('idx')} 句切成 {len(rec.get('parts') or [])} 段（{segs}）"
    if op == "text":
        return f"[{at}] 修正第 {rec.get('idx')} 句文本：{rec.get('old', '')[:20]!r} → {rec.get('text', '')[:20]!r}"
    return f"[{at}] {op}"


def block(adjustments: list, summary: dict) -> dict:
    """构造写进 .meta.json 的 edits 段：完整记录 + 归一化视图，两处并存。"""
    return {
        "version": EDIT_VERSION,
        "count": len(adjustments),
        "adjustments": adjustments,
        "summary": summary,
    }
