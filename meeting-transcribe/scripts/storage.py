"""OSS 标准库客户端：V1 上传/删除、可选私有 GET 签名和无敏感 URL 日志的读取校验。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import mimetypes
import uuid
import time
from email.utils import formatdate
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen


def _normalize_endpoint(endpoint: str) -> str:
    """去掉协议前缀与结尾斜杠，得到纯主机名形式的 endpoint。"""
    e = endpoint.strip()
    for prefix in ("https://", "http://"):
        if e.startswith(prefix):
            e = e[len(prefix):]
    return e.rstrip("/")


def _sign(secret: str, verb: str, content_md5: str, content_type: str, date: str, resource: str) -> str:
    """按 OSS 规则计算 Authorization 头中的签名部分。"""
    string_to_sign = f"{verb}\n{content_md5}\n{content_type}\n{date}\n{resource}"
    digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("utf-8")


def upload(cfg, local_path: Path, key: str | None = None) -> tuple:
    """上传本地文件到 OSS，返回 (object_key, 公网访问 URL)。

    object_key 缺省时使用随机 UUID，避免同名覆盖与文件名编码问题。
    """
    missing = cfg.missing(["oss_endpoint", "oss_access_key_id", "oss_access_key_secret", "oss_bucket"])
    if missing:
        raise RuntimeError(f"OSS 配置缺失: {', '.join(missing)}")

    if key is None:
        key = f"{uuid.uuid4().hex}{local_path.suffix or '.mp3'}"
    safe_key = quote(key, safe="/")

    endpoint = _normalize_endpoint(cfg.oss_endpoint)
    bucket = cfg.oss_bucket
    content_type = mimetypes.guess_type(key)[0] or "audio/mpeg"
    data = local_path.read_bytes()

    date = formatdate(usegmt=True)
    resource = f"/{bucket}/{key}"
    token = getattr(cfg, "oss_security_token", "")
    canonical = f"x-oss-security-token:{token}\n" if token else ""
    resource = canonical + resource
    signature = _sign(cfg.oss_access_key_secret, "PUT", "", content_type, date, resource)

    url = f"https://{bucket}.{endpoint}/{safe_key}"
    req = Request(
        url,
        data=data,
        method="PUT",
        headers={
            "Authorization": f"OSS {cfg.oss_access_key_id}:{signature}",
            "Date": date,
            "Content-Type": content_type,
            "Content-Length": str(len(data)),
        },
    )
    if token:
        req.add_header("x-oss-security-token", token)

    try:
        with urlopen(req, timeout=120) as resp:
            resp.read()
    except HTTPError as e:
        raise RuntimeError(f"OSS 上传失败 (HTTP {e.code})，请核对权限与配置") from e

    public_url = build_public_url(cfg, key)
    print(f"[oss] 已上传 -> {key}（{len(data) / 1024 / 1024:.2f} MiB）")
    return key, public_url


def build_public_url(cfg, key: str) -> str:
    """Build either the existing public URL or a time-limited private OSS read URL."""
    safe_key = quote(key, safe="/")
    if getattr(cfg, "oss_url_mode", "public") == "signed":
        return signed_url(cfg, key)
    if cfg.oss_host:
        return f"{cfg.oss_host.rstrip('/')}/{safe_key}"
    endpoint = _normalize_endpoint(cfg.oss_endpoint)
    return f"https://{cfg.oss_bucket}.{endpoint}/{safe_key}"


def signed_url(cfg, key: str) -> str:
    """Sign a V1 GET URL against the OSS origin; CDN permissions are not modified."""
    expires = str(int(time.time()) + cfg.oss_url_ttl)
    safe_key = quote(key, safe="/")
    resource = f"/{cfg.oss_bucket}/{key}"
    token = getattr(cfg, "oss_security_token", "")
    if token:
        resource += "?security-token=" + token
    signature = _sign(cfg.oss_access_key_secret, "GET", "", "", expires, resource)
    params = {"OSSAccessKeyId": cfg.oss_access_key_id, "Expires": expires, "Signature": signature}
    if token:
        params["security-token"] = token
    return f"https://{cfg.oss_bucket}.{_normalize_endpoint(cfg.oss_endpoint)}/{safe_key}?{urlencode(params)}"


def delete(cfg, key: str) -> bool:
    """删除 OSS 上的临时对象，成功返回 True。

    用于转写结束后清理上传的中间音频；失败只打印告警，不向上传播异常，
    避免清理动作反过来让整轮识别「失败」。
    """
    missing = cfg.missing(["oss_endpoint", "oss_access_key_id", "oss_access_key_secret", "oss_bucket"])
    if missing:
        print(f"[oss] 配置缺失，跳过清理: {', '.join(missing)}")
        return False

    safe_key = quote(key, safe="/")
    endpoint = _normalize_endpoint(cfg.oss_endpoint)
    bucket = cfg.oss_bucket

    date = formatdate(usegmt=True)
    resource = f"/{bucket}/{key}"
    token = getattr(cfg, "oss_security_token", "")
    if token:
        resource = f"x-oss-security-token:{token}\n" + resource
    signature = _sign(cfg.oss_access_key_secret, "DELETE", "", "", date, resource)

    url = f"https://{bucket}.{endpoint}/{safe_key}"
    req = Request(
        url,
        method="DELETE",
        headers={
            "Authorization": f"OSS {cfg.oss_access_key_id}:{signature}",
            "Date": date,
        },
    )
    if token:
        req.add_header("x-oss-security-token", token)

    try:
        with urlopen(req, timeout=60) as resp:
            resp.read()
        print(f"[oss] 已清理临时对象 -> {key}")
        return True
    except (HTTPError, URLError, TimeoutError, OSError) as e:
        # Errors must never include a signed download URL or credential-bearing body.
        code = f"HTTP {e.code}" if isinstance(e, HTTPError) else type(e).__name__
        print(f"[oss] 清理失败 ({code})，请手动删除或恢复清理: {key}")
        return False


def verify(url: str, timeout: int = 60) -> int:
    """Check readability without printing signed credentials; GET signatures require GET."""
    try:
        signed = "Signature=" in urlsplit(url).query
        req = Request(url, method="GET" if signed else "HEAD",
                      headers={"Range": "bytes=0-0"} if signed else {})
        with urlopen(req, timeout=timeout) as resp:
            length = int(resp.headers.get("Content-Length") or 0)
            if resp.status not in (200, 206):
                raise RuntimeError(f"音频地址返回 HTTP {resp.status}")
            return length
    except HTTPError as e:
        raise RuntimeError(f"音频地址不可访问 (HTTP {e.code})，请核对 OSS 访问模式与权限") from e
