#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# MrDoc 私人笔记本 —— MrDoc Token API 命令行封装
#
# MrDoc Token API client; origin and credentials come from local configuration.
# 依赖：仅 Python 3 标准库（urllib），无需 requests，可直接拷走用。
#
# 设计要点：
#   - 所有写操作（update/append/rename/move）都会先 get_doc 取原标题与所属文集，
#     再回写。原因：MrDoc 的 /api/modify_doc/ 会用传入的 title 无条件覆盖文档名，
#     字段缺失即把标题清空（0.9.6 与 1.1.1 均实测确认），所以绝不允许“只传正文”。
#   - 目录树 get_level_docs 在 1.1.1 已不限层级，但按“顶层节点”每页 20 个分页，
#     且 is_page 参数因源码用 `is not False` 严格比较而无法关闭，故必须自行翻页合并。
#   - 只读命令默认输出人类可读文本；任何命令加 --json 输出原始响应，便于程序化处理。
#   - Token / 站点地址可用 --config/--base 或环境变量 MRDOC_TOKEN / MRDOC_BASE 覆盖。
# =============================================================================

import argparse
import json
import mimetypes
import os
import sys
import uuid
from urllib import request as urlrequest
from urllib import parse as urlparse
from urllib.error import HTTPError, URLError
from local_config import config_path, load_config, resolve_secret, validate_origin

# Windows 控制台默认 GBK，中文正文会炸，这里强制 UTF-8 输出。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

DEFAULT_BASE = None
DEFAULT_TOKEN = None
TIMEOUT = 30


# ---------------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------------
def _request(path, params=None, data=None, files=None, method=None):
    """向 MrDoc 发送一次请求并返回解析后的 JSON。

    path     : 形如 'get_projects/' 的 API 相对路径
    params   : 拼进 query string 的字典（自动带上 token，由调用方传 QBASE 中的值）
    data     : 表单字段（dict），与 files 互斥/可共存
    files    : {'字段名': (文件名, bytes, 'image/png')}，存在时走 multipart
    method   : 显式指定请求方法；缺省时有 data/files 即 POST，否则 GET
    """
    base = QBASE["base"].rstrip("/")
    url = "{}/api/{}".format(base, path.lstrip("/"))
    params = dict(params or {})
    params.setdefault("token", QBASE["token"])
    url = url + ("&" if "?" in url else "?") + urlparse.urlencode(params)

    body = None
    headers = {"User-Agent": "mrdoc-skill/1.0"}
    if files:
        # 手工拼 multipart/form-data：标准库不给现成的 multipart 编码器
        boundary = "----mrdoc" + uuid.uuid4().hex
        buf = []
        for k, v in (data or {}).items():
            buf.append(
                "--{}\r\nContent-Disposition: form-data; name=\"{}\"\r\n\r\n{}\r\n".format(
                    boundary, k, v
                ).encode("utf-8")
            )
        for k, (fn, content, ctype) in files.items():
            buf.append(
                "--{}\r\nContent-Disposition: form-data; name=\"{}\"; filename=\"{}\"\r\n"
                "Content-Type: {}\r\n\r\n".format(boundary, k, fn, ctype).encode("utf-8")
            )
            buf.append(content)
            buf.append(b"\r\n")
        buf.append("--{}--\r\n".format(boundary).encode("utf-8"))
        body = b"".join(buf)
        headers["Content-Type"] = "multipart/form-data; boundary={}".format(boundary)
    elif data is not None:
        # URL 编码表单：urlencode 内部做 quote_plus，中文安全
        body = urlparse.urlencode(data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=utf-8"

    if method is None:
        method = "POST" if body is not None else "GET"
    req = urlrequest.Request(url, data=body, headers=headers, method=method)
    try:
        opener = urlrequest.build_opener(NoRedirect())
        with opener.open(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except HTTPError as exc:
        status = exc.code
        exc.close()
        die("HTTP {} calling {}. No retry performed.".format(status, path))
    except (URLError, OSError):
        die("Connection failed calling {}. Check reachability; reconcile state before retrying writes.".format(path))
    try:
        return json.loads(raw)
    except ValueError:
        # 404 / 500 时 MrDoc 返回整页 HTML，这里给出可读的定位信息
        raise SystemExit(
            "接口返回非 JSON（路径可能不存在或站点版本不支持）：{}\n{}".format(
                path, "Response body omitted"
            )
        )


def call(path, **kw):
    """带 status 校验的接口调用：status 为假时打印原因并以退出码 1 结束。"""
    r = _request(path, **kw)
    if not r.get("status"):
        die("接口 {} 返回失败：{}".format(path, json.dumps(r, ensure_ascii=False)))
    return r


def emit(obj):
    """按全局 --json 开关输出结果。"""
    if QBASE["json"]:
        print(json.dumps(obj, ensure_ascii=False, indent=2))


def die(msg):
    """Redact the active token from server errors before displaying them."""
    secret = QBASE.get("token")
    if secret:
        msg = str(msg).replace(secret, "<redacted>").replace(urlparse.quote_plus(secret), "<redacted>")
    raise SystemExit(msg)


class NoRedirect(urlrequest.HTTPRedirectHandler):
    """Prevent credentials in query strings from following redirects."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def configure(args):
    """Resolve configuration and explicitly bind saved credentials to their origin."""
    cfg = load_config("mrdoc", args.config)
    base = args.base or os.environ.get("MRDOC_BASE") or cfg.get("base_url")
    QBASE["base"] = validate_origin(base)
    configured_base = cfg.get("base_url")
    if configured_base and QBASE["base"] != validate_origin(configured_base) and not os.environ.get("MRDOC_TOKEN"):
        die("Changing origin requires MRDOC_TOKEN for that origin or a separate --config file.")
    QBASE["token"] = resolve_secret(cfg, "token", "MRDOC_TOKEN")
    QBASE["json"] = args.json
    return cfg


def default_project(cfg):
    """Resolve the configured project name against live metadata instead of fixed IDs."""
    name = cfg.get("default_project_name")
    pid = cfg.get("default_project_id")
    projects = call("get_projects/")["data"]
    matches = [p for p in projects if p.get("name") == name] if name else [p for p in projects if p.get("id") == pid]
    if len(matches) != 1:
        die("Default project is missing or ambiguous; choose --pid from projects or configure a unique default_project_name.")
    return matches[0]["id"]


# ---------------------------------------------------------------------------
# 数据获取辅助
# ---------------------------------------------------------------------------
def get_doc(did):
    """取单篇文档的完整信息（含 md_content / name / top_doc）。"""
    return call("get_doc/", params={"did": did})["data"]


def fetch_self_docs(limit=200, kw=""):
    """拉取“我创建的”全部文档（自动翻页）。

    MrDoc 的 get_self_docs 每页默认只回 10 条且不返回总数，
    因此这里以“本页条数 < limit”作为终止条件。
    """
    out, page = [], 1
    while True:
        params = {"limit": limit, "page": page}
        if kw:
            params["kw"] = kw
        batch = call("get_self_docs/", params=params)["data"]
        out.extend(batch)
        if len(batch) < limit or page > 50:
            break
        page += 1
    return out


def fetch_docs(pid, kw="", limit=100):
    """拉取文集下的文档列表（自动翻页，可选服务端关键词）。

    get_docs 不返回总数，只能以「本页条数 < limit」判断是否还有下一页。
    1.1.1 起该接口返回文集下全部已发布文档（含他人创建），不再只限本人。
    """
    out, page = [], 1
    while True:
        params = {"pid": pid, "limit": limit, "page": page}
        if kw:
            params["kw"] = kw
        batch = call("get_docs/", params=params)["data"]
        out.extend(batch)
        if len(batch) < limit or page > 50:
            break
        page += 1
    return out


def fetch_level_docs(pid):
    """拉取文集的完整目录树（自动翻页合并）。

    get_level_docs 从 1.1.1 起不再限制三级，但存在两个必须处理的行为：
    1. 分页单位是「顶层节点」，每页 20 个——165 篇文档的文集若顶层超过 20 个，
       只取第一页会漏掉整棵子树；
    2. 源码里 `is_page = request.GET.get('is_page', True)` 配 `if is_page is False`，
       query string 传进来永远是字符串，`is False` 恒不成立，所以**无法通过参数关闭分页**。
    因此这里按 total_page 自行翻页：各页的顶层节点互不重叠，直接拼接即可。
    """
    out, page = [], 1
    while True:
        r = call("get_level_docs/", params={"pid": pid, "page": page})
        out.extend(r["data"])
        if page >= (r.get("total_page") or 1):
            break
        page += 1
    return out


def count_nodes(nodes, child_key="sub"):
    """统计树里的节点总数（含各级子节点）。get_level_docs 的 total 只给顶层数，不能直接用。"""
    return sum(1 + count_nodes(n.get(child_key) or [], child_key) for n in nodes)


def read_body(args):
    """从 --file / --text / stdin 读正文，保证写进文档的内容是 UTF-8 Markdown。"""
    if getattr(args, "text", None) is not None:
        return args.text
    if getattr(args, "file", None):
        with open(args.file, "r", encoding="utf-8-sig") as f:
            return f.read()
    if not sys.stdin.isatty():
        return sys.stdin.read()
    die("缺少正文：用 --file <md文件> 或 --text <内容> 提供")


# ---------------------------------------------------------------------------
# 命令实现
# ---------------------------------------------------------------------------
def cmd_token(args):
    """校验 Token 并显示当前身份（is_writer / user_type）。"""
    r = call("check_token/")
    d = r["data"]
    emit(r)
    print("身份：{}（{}）  可写：{}".format(d.get("username"), d.get("user_type"), d.get("is_writer")))
    print("站点：{}".format(QBASE["base"]))


def cmd_projects(args):
    """列出可见文集（自己创建 + 被协作的）。"""
    params = {}
    if args.kw:
        params["kw"] = args.kw
    r = call("get_projects/", params=params)
    emit(r)
    rows = r["data"]
    # 制表符分隔而非空格对齐：中文在不同终端里宽度不一，空格对齐必然错位
    print("PID\t文集\t文档数\t简介")
    for p in rows:
        print("{}\t{}\t{}\t{}".format(p["id"], p["name"], p.get("total", ""), (p.get("desc") or "")[:40]))
    print("共 {} 个文集".format(len(rows)))


def cmd_tree(args):
    """打印文集目录树（1.1.1 起不限层级，自动翻页取全量）。"""
    roots = fetch_level_docs(args.pid)
    emit({"data": roots})
    max_depth = [0]

    def walk(nodes, depth):
        """递归输出节点，同时记录实际出现的最大层级。"""
        for n in nodes:
            print("{}{}  [did={} mode={}]".format("  " * depth, n["name"], n["id"], n.get("editor_mode")))
            max_depth[0] = max(max_depth[0], depth + 1)
            walk(n.get("sub") or [], depth + 1)

    walk(roots, 0)
    print("顶层 {} 个，节点合计 {} 个，最大层级 {}".format(
        len(roots), count_nodes(roots), max_depth[0]))


def cmd_docs(args):
    """列出文集下的已发布文档（1.1.1 起含他人创建的同文集文档）。"""
    rows = fetch_docs(args.pid, kw=args.kw or "", limit=args.limit)
    if args.json:
        emit(rows)
        return
    start = (args.page - 1) * args.limit
    for d in rows[start:start + args.limit]:
        print("{}\t{}\t上级={}\t{}\t{}".format(
            d["id"], d["name"], d["parent_doc"], d["modify_time"][:16], d.get("create_user", "")))
    print("共 {} 篇".format(len(rows)))


def cmd_mydocs(args):
    """列出我创建的全部文档，可按关键词过滤（服务端匹配标题+正文）。"""
    rows = fetch_self_docs(limit=args.limit, kw=args.kw or "")
    if args.json:
        emit(rows)
        return
    for d in rows:
        print("{}\t{}\t文集={}\t{}".format(d["id"], d["name"], d["project_name"], d["modify_time"][:16]))
    print("共 {} 篇".format(len(rows)))


def cmd_get(args):
    """输出单篇文档内容：默认 Markdown 原文，--html 输出渲染后 HTML。"""
    d = get_doc(args.did)
    if args.json:
        emit(d)
        return
    if args.html:
        print(d.get("content") or "")
        return
    md = d.get("md_content")
    if md is None:
        # 富文本(editor_mode=3)文档没有 pre_content，只能回退到 HTML
        sys.stderr.write("提示：该文档为富文本模式，无 Markdown 原文，已输出 HTML\n")
        print(d.get("content") or "")
        return
    print(md)


def cmd_search(args):
    """按关键词搜索文档。

    1.1.1 的 get_self_docs / get_docs 都支持服务端 kw（同时匹配标题与正文），
    所以默认只需一次请求就能拿到候选集，不必再把所有文档拉下来本地比对。
    代价是服务端只返回摘要、不说明命中位置；需要定位时加 --content，
    对候选逐篇拉正文并输出命中的那一行。
    """
    if args.pid:
        rows = fetch_docs(args.pid, kw=args.kw, limit=args.limit)
        scope = "文集 {}".format(args.pid)
        name_fallback = str(args.pid)
    else:
        rows = fetch_self_docs(limit=args.limit, kw=args.kw)
        scope = "我创建的文档"
        name_fallback = "?"

    kw_low = args.kw.lower()
    hits = []
    for d in rows:
        name_hit = kw_low in d["name"].lower()
        detail = ""
        if args.content:
            try:
                md = get_doc(d["id"]).get("md_content") or ""
            except SystemExit:
                # get_doc 只允许读自己创建的文档，他人文档拿不到正文，跳过定位
                md = ""
            for line in md.splitlines():
                if kw_low in line.lower():
                    detail = line.strip()[:90]
                    break
        if name_hit and detail:
            how = "标题+正文"
        elif name_hit:
            how = "标题"
        else:
            how = "正文"
        hits.append((d, how, detail))

    if args.json:
        emit([{"id": d["id"], "name": d["name"],
               "project": d.get("project_name") or name_fallback,
               "hit": how, "line": detail} for d, how, detail in hits])
        return
    for d, how, detail in hits:
        # get_docs 的结果不带 project_name，用 pid 兜底，避免打印成 "?"
        proj = d.get("project_name") or name_fallback
        print("{}\t{}\t文集={}\t命中={}{}".format(
            d["id"], d["name"], proj, how, ("\t" + detail) if detail else ""))
    print("命中 {} 篇（搜索范围：{}）".format(len(hits), scope))


def cmd_create(args):
    """新建文档。pid 必须是“自己创建”的文集（协作文集会被接口拒绝）。"""
    body = read_body(args)
    data = {"pid": args.pid, "title": args.title, "doc": body, "editor_mode": args.editor_mode}
    if args.parent:
        data["parent_doc"] = args.parent
    r = call("create_doc/", data=data)
    did = r["data"]
    emit(r)
    print("已创建：did={}  {}  {}".format(did, args.title, doc_url(args.pid, did)))


def doc_url(pid, did):
    """拼出站内可访问地址（MrDoc 的规范路径是 /project-<pid>/doc-<did>/）。"""
    return "{}/project-{}/doc-{}/".format(QBASE["base"].rstrip("/"), pid, did)


def _rewrite(did, body, new_title=None, parent=None):
    """内部函数：以“读原文→改字段→整篇回写”的方式更新文档。

    必须在请求里带上 pid 与 title：modify_doc 不校验缺省值，
    缺 title 会把文档名改成空串（0.9.6 与 1.1.1 均实测复现），缺 pid 会报非法请求。
    反过来 parent_doc 是安全的：1.1.1 源码里传空值时保留原 parent_doc，
    所以不改层级时不必显式回填。
    """
    cur = get_doc(did)
    if cur.get("editor_mode") not in (1, 2):
        die("该文档不是 Markdown 模式（1/2），已停止更新，避免覆盖富文本或表格")
    pid = cur["top_doc"]
    title = new_title if new_title else cur["name"]
    if not title:
        die("原标题为空，请用 --title 显式指定，否则会把文档名写成空")
    data = {"pid": pid, "did": did, "title": title, "doc": body}
    if parent is not None:
        data["parent_doc"] = parent
    call("modify_doc/", data=data)
    return pid, title


def cmd_update(args):
    """整篇替换正文（可顺带改标题）。每次修改 MrDoc 都会自动写一条历史版本。"""
    body = read_body(args)
    pid, title = _rewrite(args.did, body, new_title=args.title)
    print("已更新：did={}  {}  {}".format(args.did, title, doc_url(pid, args.did)))


def cmd_append(args):
    """在正文末尾追加内容（不覆盖原有内容）。"""
    cur = get_doc(args.did)
    old = cur.get("md_content") or ""
    add = read_body(args)
    joiner = "\n" if old and not old.endswith("\n") else ""
    body = old + joiner + add
    pid, title = _rewrite(args.did, body)
    print("已追加：did={}  {}（{} → {} 字符）".format(args.did, title, len(old), len(body)))


def cmd_rename(args):
    """只改标题，正文原样保留。"""
    cur = get_doc(args.did)
    pid, title = _rewrite(args.did, cur.get("md_content") or "", new_title=args.title)
    print("已改名：did={}  {} → {}".format(args.did, cur["name"], title))


def cmd_move(args):
    """调整文档层级（parent_doc），--parent 0 表示置为一级文档。

    1.1.1 服务端新增了环检测：把文档挂到它自己的下级会被拒绝，
    返回「不能将文档的上级文档设置为其下级文档」。
    """
    cur = get_doc(args.did)
    try:
        pid, title = _rewrite(args.did, cur.get("md_content") or "", parent=args.parent)
    except SystemExit as e:
        if "下级文档" in str(e):
            die("移动被拒绝：目标上级 {} 是它自己的下级文档，会形成环".format(args.parent))
        raise
    print("已移动：did={}  {}  → 上级 {}".format(args.did, title, args.parent))


def cmd_delete(args):
    """删除文档。MrDoc 是软删除（status=3），接口层无恢复入口，误删需登录网页端处理。"""
    if not args.yes:
        die("删除不可通过 API 撤销。确认请加 --yes")
    cur = get_doc(args.did)
    call("delete_doc/", data={"did": args.did})
    print("已删除（软删除）：did={}  {}".format(args.did, cur["name"]))


def cmd_upload(args):
    """上传图片到素材库，输出可直接写进 Markdown 的图片地址。"""
    with open(args.file, "rb") as f:
        content = f.read()
    fn = os.path.basename(args.file)
    ext = os.path.splitext(fn)[1].lower()
    ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml"}.get(ext, "application/octet-stream")
    r = _request("upload_img/", files={"image": (fn, content, ctype)})
    emit(r)
    if r.get("success") == 1 or r.get("status"):
        url = r.get("url") or r.get("data")
        print("图片地址：{}".format(url))
        print("Markdown：![]({})".format(url))
    else:
        die("上传失败：{}".format(json.dumps(r, ensure_ascii=False)))


def cmd_attach(args):
    """上传附件（非图片资源）到站点附件库，输出下载地址。

    1.1.1 新增的能力，但有两条硬限制来自服务端，不是脚本能绕开的：
    1. 后缀受站点设置 `attachment_suffix` 白名单约束，默认只允许 zip，
       传 .md/.pdf 等会直接返回「不支持的格式」；
    2. 配套的附件列表接口 get_attachments 在 1.1.1 源码里漏 import Attachment，
       恒抛 NameError 返回「系统异常」，所以传完无法通过 API 列出来。
    """
    with open(args.file, "rb") as f:
        content = f.read()
    fn = os.path.basename(args.file)
    ctype = mimetypes.guess_type(fn)[0] or "application/octet-stream"
    r = _request("upload_file/", files={"attachment_upload": (fn, content, ctype)})
    emit(r)
    if not r.get("status"):
        die("上传失败：{}（若提示「不支持的格式」，是站点未放行该后缀）"
            .format(json.dumps(r, ensure_ascii=False)))
    data = r.get("data") or {}
    path = str(data.get("url", ""))
    url = path if path.startswith("http") else "{}{}".format(
        QBASE["base"].rstrip("/"), path if path.startswith("/") else "/" + path)
    print("附件：{}".format(data.get("name")))
    print("地址：{}".format(url))


def cmd_url(args):
    """打印文档的站内访问地址。"""
    pid = args.pid or get_doc(args.did)["top_doc"]
    print(doc_url(pid, args.did))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
QBASE = {"base": DEFAULT_BASE, "token": DEFAULT_TOKEN, "json": False}


def build_parser():
    """构造 CLI 参数树。所有子命令共享 --base/--config/--json。"""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--base", default=None, help="站点 HTTP(S) origin；默认取本地配置或 MRDOC_BASE")
    common.add_argument("--config", help="本机 JSON 配置路径；密钥通过配置或环境变量提供")
    common.add_argument("--json", action="store_true", help="输出原始 JSON")

    # 注意：--base/--config/--json 只挂在各子命令上。若同时挂到顶层解析器，
    # 子解析器的默认值会把顶层已解析的值覆盖成 None（argparse 的已知行为）。
    p = argparse.ArgumentParser(
        prog="mrdoc.py",
        description="MrDoc 私人笔记本（MrDoc）API 客户端（全局参数请写在子命令之后）",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, func, help_text, **kw):
        sp = sub.add_parser(name, parents=[common], help=help_text, **kw)
        sp.set_defaults(func=func)
        return sp

    add("token", cmd_token, "校验 Token 与身份")

    sp = add("projects", cmd_projects, "列出可见文集")
    sp.add_argument("--kw", default="", help="按名称/简介过滤")

    sp = add("tree", cmd_tree, "打印文集目录树（不限层级，自动翻页）")
    sp.add_argument("--pid", type=int, required=True)

    sp = add("docs", cmd_docs, "列出文集下的文档（含他人创建）")
    sp.add_argument("--pid", type=int, required=True)
    sp.add_argument("--kw", default="", help="服务端按标题+正文过滤")
    sp.add_argument("--limit", type=int, default=100)
    sp.add_argument("--page", type=int, default=1)

    sp = add("mydocs", cmd_mydocs, "列出我创建的全部文档")
    sp.add_argument("--kw", default="", help="服务端按标题+正文过滤")
    sp.add_argument("--limit", type=int, default=200)

    sp = add("get", cmd_get, "读取文档内容（默认 Markdown）")
    sp.add_argument("--did", type=int, required=True)
    sp.add_argument("--html", action="store_true", help="输出渲染后的 HTML")

    sp = add("search", cmd_search, "按关键词搜索（服务端匹配标题+正文）")
    sp.add_argument("--kw", required=True)
    sp.add_argument("--pid", type=int, default=None,
                    help="限定某个文集（不传则搜自己创建的全部文档）")
    sp.add_argument("--limit", type=int, default=200)
    sp.add_argument("--content", action="store_true",
                    help="额外拉正文，输出命中的那一行（较慢）")

    sp = add("create", cmd_create, "新建文档")
    sp.add_argument("--pid", type=int, help="所属文集 ID；省略时实时解析本机配置的默认文集")
    sp.add_argument("--title", required=True)
    sp.add_argument("--file", help="正文 Markdown 文件")
    sp.add_argument("--text", help="正文内容（与 --file 二选一）")
    sp.add_argument("--parent", type=int, default=0, help="上级文档 ID，默认 0（一级）")
    sp.add_argument("--editor-mode", type=int, default=1, help="1=Markdown 3=富文本")

    sp = add("update", cmd_update, "整篇替换文档正文")
    sp.add_argument("--did", type=int, required=True)
    sp.add_argument("--title", help="同时改标题（不传则保留原标题）")
    sp.add_argument("--file")
    sp.add_argument("--text")

    sp = add("append", cmd_append, "在文末追加内容")
    sp.add_argument("--did", type=int, required=True)
    sp.add_argument("--file")
    sp.add_argument("--text")

    sp = add("rename", cmd_rename, "只改文档标题")
    sp.add_argument("--did", type=int, required=True)
    sp.add_argument("--title", required=True)

    sp = add("move", cmd_move, "调整文档层级")
    sp.add_argument("--did", type=int, required=True)
    sp.add_argument("--parent", type=int, required=True)

    sp = add("delete", cmd_delete, "删除文档（软删除，不可逆）")
    sp.add_argument("--did", type=int, required=True)
    sp.add_argument("--yes", action="store_true")

    sp = add("upload", cmd_upload, "上传图片并输出地址")
    sp.add_argument("--file", required=True)

    sp = add("attach", cmd_attach, "上传附件（受站点后缀白名单限制）")
    sp.add_argument("--file", required=True)

    sp = add("url", cmd_url, "打印文档访问地址")
    sp.add_argument("--did", type=int, required=True)
    sp.add_argument("--pid", type=int, default=None)

    return p


def main():
    args = build_parser().parse_args()
    cfg = configure(args)
    if args.cmd == "create" and args.pid is None:
        args.pid = default_project(cfg)
    args.func(args)


if __name__ == "__main__":
    main()
