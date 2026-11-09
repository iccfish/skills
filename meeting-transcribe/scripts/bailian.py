"""
bailian.py —— 阿里云百炼非实时语音识别（filetrans 异步任务）客户端。

三个动作：提交转写任务、轮询任务状态、下载结果 JSON。
只依赖标准库 urllib；说话人分离通过 parameters.diarization_enabled 开启。
"""

from __future__ import annotations

import json
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

# 终态集合，轮询到即停止
TERMINAL = {"SUCCEEDED", "FAILED", "UNKNOWN", "CANCELED"}


class TaskFailed(RuntimeError):
    """A confirmed terminal cloud failure, safe for terminal-state cleanup."""


class HTTPFailure(RuntimeError):
    """Retain HTTP status without leaking response URLs or authentication material."""

    def __init__(self, status: int):
        self.status = status
        super().__init__(f"百炼接口返回 HTTP {status}")


def _request(method: str, url: str, api_key: str, body: dict | None = None,
             extra_headers: dict | None = None, timeout: int = 120) -> dict:
    """发起 HTTP 请求并解析 JSON 响应，非 2xx 时抛出带响应体的异常。"""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    headers.update(extra_headers or {})
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = Request(url, data=data, method=method, headers=headers)
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except HTTPError as e:
        raise HTTPFailure(e.code) from e


def submit(cfg, audio_url: str, diarization: bool = True) -> str:
    """提交异步转写任务，返回 task_id。"""
    url = f"{cfg.dashscope_base.rstrip('/')}/api/v1/services/audio/asr/transcription"
    body = {
        "model": cfg.model,
        "input": {"file_urls": [audio_url]},
        "parameters": {
            "channel_id": [0],
            "language_hints": cfg.language_hints,
        },
    }
    if diarization:
        # filetrans 的说话人分离参数名是 diarization_enabled（同步接口叫 speaker_diarization_enabled）
        body["parameters"]["diarization_enabled"] = True

    reply = _request("POST", url, cfg.api_key, body, {"X-DashScope-Async": "enable"})
    output = reply.get("output") or {}
    task_id = output.get("task_id")
    if not task_id:
        raise RuntimeError("提交响应未返回 task_id；提交结果未知，请勿直接重提")
    print(f"[asr] 任务已提交: task_id={task_id} 状态={output.get('task_status')}")
    return task_id


def wait(cfg, task_id: str, on_status=None) -> dict:
    """轮询任务直到终态，返回完整查询响应。"""
    url = f"{cfg.dashscope_base.rstrip('/')}/api/v1/tasks/{task_id}"
    started = time.monotonic()
    last = ""

    while True:
        remaining = cfg.max_wait - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError(f"等待超时，任务状态未知，task_id={task_id}；请使用 --resume")
        reply = _request("GET", url, cfg.api_key, timeout=min(120, remaining))
        output = reply.get("output") or {}
        status = output.get("task_status", "")
        if on_status:
            on_status(status)
        if status != last:
            print(f"[asr] 任务状态: {status}")
            last = status

        if status in TERMINAL:
            if status == "UNKNOWN":
                raise RuntimeError(f"任务暂不可查询，保留恢复记录：task_id={task_id}")
            if status != "SUCCEEDED":
                raise TaskFailed(f"转写任务失败: {status}，task_id={task_id}")
            # 子任务失败时整体仍可能是 SUCCEEDED，这里单独检查
            for r in output.get("results") or []:
                if r.get("subtask_status") not in (None, "SUCCEEDED"):
                    raise TaskFailed(f"转写子任务失败: {r.get('code', 'unknown')}，task_id={task_id}")
            return output

        time.sleep(min(cfg.poll_interval, max(0, cfg.max_wait - (time.monotonic() - started))))


def fetch_transcription(output: dict) -> dict:
    """从任务结果里取 transcription_url 并下载结果 JSON。"""
    results = output.get("results") or []
    if not results:
        raise RuntimeError(f"任务结果为空: {json.dumps(output, ensure_ascii=False)[:300]}")
    url = results[0].get("transcription_url")
    if not url:
        raise RuntimeError("结果中缺少 transcription_url")
    with urlopen(Request(url, method="GET"), timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))
