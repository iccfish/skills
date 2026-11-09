"""Durable filetrans jobs: reserve outputs, resume reads, and clean up by state."""
from __future__ import annotations

import time
import uuid
import re
import hashlib
import json
from pathlib import Path

import bailian
import media
import storage
from localio import atomic_json, file_lock, read_json


def _save(path: Path, state: dict) -> None:
    """Checkpoint only local identifiers and state, never API keys or signed URLs."""
    state["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    atomic_json(path, state)


def reserve(src: Path, cfg, diarization: bool, keep_oss: bool) -> tuple:
    """Reserve a complete output group before starting a billable operation."""
    src = Path(src).resolve()
    with file_lock(src.with_name(src.name + ".allocation")):
        # A repeated original command must not create a second billable task after a timeout.
        pattern = re.compile(re.escape(src.stem) + r"(?:-\d+)?\.task\.json$")
        for path in src.parent.glob("*.task.json"):
            if not pattern.fullmatch(path.name):
                continue
            prior = read_json(path)
            if prior.get("source") == str(src) and prior.get("phase") not in ("SAVED", "FAILED", "UPLOAD_FAILED"):
                raise RuntimeError(f"已有未完成任务，请先恢复而非重提: --resume {path}")
        group = media.output_group(src)
        state = {
            "schema": 1, "source": str(src), "model": cfg.model,
            "diarization": diarization, "api_origin": cfg.dashscope_base.rstrip("/"),
            "oss_endpoint": storage._normalize_endpoint(cfg.oss_endpoint), "oss_bucket": cfg.oss_bucket,
            "phase": "PREPARED", "task_id": None, "object_key": None,
            "cleanup": "not_uploaded", "keep_oss": keep_oss,
            "outputs": {k: str(v) for k, v in group.items()},
        }
        atomic_json(group["task"], state, overwrite=False)
        print(f"[task] 恢复记录: {group['task']}")
    return group, state


def _cleanup(cfg, state_path: Path, state: dict) -> None:
    """Cleanup failures are checkpoints, not failures of a completed transcription."""
    if not state.get("object_key") or state.get("cleanup") == "deleted":
        return
    if state.get("keep_oss"):
        state["cleanup"] = "kept"
    else:
        try:
            state["cleanup"] = "deleted" if storage.delete(cfg, state["object_key"]) else "pending"
        except Exception as exc:
            state["cleanup"] = "pending"
            print(f"[oss] 清理异常 {type(exc).__name__}，结果保留；可用 --resume 重试清理")
    try:
        _save(state_path, state)
    except OSError:
        print("[task] 清理状态保存失败；原始转写结果已保留，请核查任务记录")


def _validate_origin(cfg, state: dict) -> None:
    """Never query or delete an old task against a different configured account origin."""
    expected = (cfg.dashscope_base.rstrip("/"), storage._normalize_endpoint(cfg.oss_endpoint), cfg.oss_bucket)
    recorded = (state.get("api_origin"), state.get("oss_endpoint"), state.get("oss_bucket"))
    if expected != recorded:
        raise ValueError("恢复配置的百炼地址/OSS Endpoint/Bucket 与任务记录不一致，请使用原配置")


def load(state_path: Path, cfg) -> tuple:
    """Validate output paths before trusting a local recovery manifest."""
    state_path = Path(state_path).resolve()
    state = read_json(state_path)
    if state.get("schema") != 1 or not isinstance(state.get("outputs"), dict):
        raise ValueError("任务记录格式错误")
    group = {k: Path(v).resolve() for k, v in state["outputs"].items()}
    expected = media.output_group(Path(str(state_path)[:-len(".task.json")] + ".asr.json"), existing=True)
    if not state_path.name.endswith(".task.json") or group != expected or group.get("task") != state_path:
        raise ValueError("任务产物路径与记录位置不匹配；请保持整组产物位置不变")
    _validate_origin(cfg, state)
    return group, state


def execute(cfg, state_path: Path, state: dict, group: dict, audio: Path | None = None) -> dict:
    """Resume known tasks without resubmission, saving results before cleanup."""
    state_path = Path(state_path)
    _validate_origin(cfg, state)
    if group["json"].exists() and state.get("phase") in ("SAVING", "SAVED"):
        data = read_json(group["json"])
        if state.get("result_sha256") != _result_hash(data):
            raise ValueError("原始结果与任务保存摘要不匹配，已保留文件和 OSS 对象，请先核对")
        state["phase"] = "SAVED"
        _save(state_path, state)
        _cleanup(cfg, state_path, state)
        return data
    phase = state.get("phase")
    if phase in ("UPLOAD_FAILED", "UPLOADING", "PREPARED") and audio is None:
        state["phase"] = "UPLOAD_FAILED"
        _cleanup(cfg, state_path, state)
        raise RuntimeError("任务未提交；已尝试清理临时对象。核对后可重新处理原始媒体")
    if phase in ("SUBMITTING", "SUBMIT_UNKNOWN") and not state.get("task_id"):
        raise RuntimeError("提交结果未知且没有 task_id；请先在云端核实，不能自动重提。对象 key 已保留在任务记录")
    if phase == "FAILED":
        _cleanup(cfg, state_path, state)
        raise bailian.TaskFailed("任务已确认失败，未重新提交；清理状态见任务记录")
    if not state.get("task_id"):
        if phase != "PREPARED" or audio is None:
            raise RuntimeError("任务尚未提交；请使用原始媒体重新启动，或先处理记录中的临时对象")
        state["object_key"] = uuid.uuid4().hex + (audio.suffix or ".mp3")
        state["audio"] = str(audio.resolve())
        state["cleanup"] = "pending"
        state["phase"] = "UPLOADING"
        _save(state_path, state)
        try:
            _, url = storage.upload(cfg, audio, key=state["object_key"])
            storage.verify(url)
        except Exception:
            state["phase"] = "UPLOAD_FAILED"
            _save(state_path, state)
            _cleanup(cfg, state_path, state)
            raise
        state["phase"] = "SUBMITTING"
        _save(state_path, state)
        try:
            task_id = bailian.submit(cfg, url, diarization=state["diarization"])
        except Exception as exc:
            rejected = isinstance(exc, bailian.HTTPFailure) and 400 <= exc.status < 500 and exc.status != 408
            state["phase"] = "FAILED" if rejected else "SUBMIT_UNKNOWN"
            _save(state_path, state)
            if rejected:
                _cleanup(cfg, state_path, state)
            raise
        state["task_id"] = task_id
        state["phase"] = "SUBMITTED"
        _save(state_path, state)

    def status_changed(status: str) -> None:
        """Record progress without treating a query error as a cloud task failure."""
        if state.get("cloud_status") != status:
            state["cloud_status"] = status
            _save(state_path, state)

    try:
        output = bailian.wait(cfg, state["task_id"], on_status=status_changed)
    except bailian.TaskFailed:
        state["phase"] = "FAILED"
        _save(state_path, state)
        _cleanup(cfg, state_path, state)
        raise
    # Any poll/download error leaves task_id and object intact for --resume.
    state["phase"] = "FETCHING"
    _save(state_path, state)
    data = bailian.fetch_transcription(output)
    state["phase"] = "SAVING"
    state["result_sha256"] = _result_hash(data)
    _save(state_path, state)
    atomic_json(group["json"], data, overwrite=False)
    state["phase"] = "SAVED"
    _save(state_path, state)
    _cleanup(cfg, state_path, state)
    return data


def _result_hash(data: dict) -> str:
    """Bind a recovered result to its task, including interruption during atomic publication."""
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
