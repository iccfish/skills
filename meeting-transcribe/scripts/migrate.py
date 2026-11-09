"""Explicit, offline migration of personal configuration and voiceprint data."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

from config import _read_json
from localio import atomic_json, atomic_text, file_lock, read_json
from paths import SKILL_ROOT, config_path, data_dir
from review import fingerprint


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def legacy_dirs():
    roots = [SKILL_ROOT, Path.home() / ".local/share/agent-skills/meeting-transcribe"]
    if os.environ.get("XDG_DATA_HOME"):
        roots.append(Path(os.environ["XDG_DATA_HOME"]) / "agent-skills/meeting-transcribe")
    if os.environ.get("LOCALAPPDATA"):
        local = Path(os.environ["LOCALAPPDATA"])
        roots.append(local / "agent-skills/meeting-transcribe")
        # Desktop-app virtualization can leave absolute references to this alias.
        roots.extend((local / "Packages").glob("OpenAI.Codex_*/LocalCache/Local/agent-skills/meeting-transcribe"))
    return roots


class Migration:
    def __init__(self, roots, source_config=None, meeting_dirs=()):
        self.target = data_dir().resolve()
        self.config = config_path().resolve()
        self.roots = list(dict.fromkeys(Path(p).resolve() for p in roots
                                       if Path(p).resolve() != self.target))
        self.sources = {}  # Original bytes also serve as concurrency guards and backups.
        self.outputs = {}
        self.manifests = {}
        self.mapping = {}
        self.meeting_dirs = set(Path(p).resolve() for p in meeting_dirs)
        self.source_config = Path(source_config).resolve() if source_config else None

    def read(self, path):
        path = Path(path).resolve()
        raw = path.read_bytes()
        self.sources[path] = raw
        return raw

    def add(self, target, raw):
        if target in self.outputs and self.outputs[target] != raw:
            raise RuntimeError(f"来源内容冲突，未迁移: {target}")
        self.outputs[target] = raw

    def remap(self, value):
        if isinstance(value, dict):
            return {key: self.remap(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.remap(item) for item in value]
        if isinstance(value, str) and Path(value).is_absolute():
            path = Path(value).resolve()
            if path in self.mapping:
                return str(self.mapping[path])
            # Only known data paths move; media and model/tool paths stay unchanged.
            for root in self.roots:
                if path.is_relative_to(root / "声纹采样"):
                    return str(self.target / path.relative_to(root))
        return value

    def plan(self):
        pending = []
        for root in self.roots:
            files = [root / "speakers.json", *root.glob("voiceprints*.json")]
            files.extend((root / "声纹采样").rglob("*.mp3"))
            for path in files:
                if not path.is_file():
                    continue
                raw = self.read(path)
                name = path.relative_to(root)
                if path.name == "voiceprints.json":
                    db = read_json(path)
                    if db.get("schema", 1) < 2:
                        name = Path("voiceprints-legacy-v1.json")
                destination = self.target / name
                self.mapping[path.resolve()] = destination
                pending.append((path, destination, raw))
        for path, destination, raw in pending:
            if path.suffix == ".json":
                document = read_json(path)
                if path.name.startswith("voiceprints"):
                    for person in document.get("speakers", []):
                        for sample in person.get("samples", []):
                            source = Path(sample.get("source", ""))
                            if source.is_absolute() and not any(source.is_relative_to(r) for r in self.roots):
                                self.meeting_dirs.add(source.parent)
                updated = self.remap(document)
                raw = encoded(updated) if updated != document else raw
            self.add(destination, raw)

        if self.source_config and self.source_config.is_file() and self.source_config != self.config:
            self.read(self.source_config)
            cfg = self.remap(dict(_read_json(self.source_config)))
            if not cfg.get("roster_path"):
                cfg["roster_path"] = str(self.target / "speakers.json")
            if not cfg.get("vp_db"):
                # Preserve old incompatible embeddings separately; never mix frontends.
                active = self.target / "voiceprints-kaldi-v2.json"
                cfg["vp_db"] = str(active if active in self.outputs or active.exists()
                                   else self.target / "voiceprints.json")
            self.add(self.config, encoded(cfg))

        for directory in sorted(self.meeting_dirs):
            for path in directory.glob("*.samples.json"):
                before = read_json(path)
                after = self.remap(before)
                if after == before:
                    continue
                after["context"] = fingerprint([after["revision"], str(Path(after["audio"]).resolve()),
                                                 after["audio_sha256"],
                                                 str(Path(after["db"]).parent / "声纹采样")])
                # Identity, review revision, decisions, vectors and sample hashes stay intact.
                for group in after.get("groups", []):
                    for item in group.get("files", []):
                        if group.get("status") == "rejected":
                            continue
                        clip = Path(item["path"])
                        content = self.outputs.get(clip)
                        if content is None:
                            content = clip.read_bytes()
                        if hashlib.sha256(content).hexdigest() != item["sha256"]:
                            raise RuntimeError(f"采样摘要不符，未迁移: {path}")
                self.read(path)
                self.manifests[path] = encoded(after)
        self.check()
        return self

    def check(self):
        for path, raw in self.outputs.items():
            if path.exists() and path.read_bytes() != raw:
                raise RuntimeError(f"目标已有不同内容，未覆盖: {path}")
        for path, raw in self.sources.items():
            if path.read_bytes() != raw:
                raise RuntimeError(f"来源已变化，请重新规划迁移: {path}")
            if path.with_name(path.name + ".lock").exists():
                raise RuntimeError(f"来源正在使用，请结束操作后迁移: {path}")

    def apply(self):
        if not self.sources:
            return None
        with file_lock(self.target / "migration"):
            self.check()
            backup = self.target / "migration-backups" / uuid.uuid4().hex
            backup.mkdir(parents=True)
            index = []
            for number, (path, raw) in enumerate(self.sources.items()):
                saved = backup / f"{number:04d}{path.suffix}"
                saved.write_bytes(raw)
                if saved.read_bytes() != raw:
                    raise RuntimeError("备份校验失败，未删除来源")
                index.append({"source": str(path), "backup": saved.name,
                              "sha256": hashlib.sha256(raw).hexdigest()})
            atomic_json(backup / "index.json", {"files": index})
            for path, raw in self.outputs.items():
                if not path.exists():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open("xb") as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
            for path, raw in self.outputs.items():
                if path.read_bytes() != raw:
                    raise RuntimeError(f"目标校验失败，未删除来源: {path}")
            self.check()
            for path, raw in self.manifests.items():
                with file_lock(path):
                    if path.read_bytes() != self.sources[path]:
                        raise RuntimeError(f"采样清单已变化: {path}")
                    atomic_text(path, raw.decode("utf-8"))
            # Complete all writes and checks before removing any original data.
            for path, raw in self.manifests.items():
                if path.read_bytes() != raw:
                    raise RuntimeError("采样清单校验失败，未删除来源")
            for path, raw in self.sources.items():
                if path in self.manifests:
                    continue
                # Aliases may refer to the same physical file, already removed once.
                if path.exists():
                    if path.read_bytes() != raw:
                        raise RuntimeError(f"来源已变化，保留原文件: {path}")
                    path.unlink()
            return backup


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="执行；默认仅检查和列出数量")
    parser.add_argument("--source-config", type=Path)
    parser.add_argument("--source-data", type=Path, action="append", default=[])
    parser.add_argument("--meeting-dir", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    try:
        source = args.source_config
        if source is None:
            candidates = [p for p in (SKILL_ROOT / "config.json", Path.home() / ".workbuddy/meeting-transcribe.json") if p.is_file()]
            if len(candidates) > 1:
                raise RuntimeError("发现多个旧配置，请用 --source-config 选择后分别处理")
            source = candidates[0] if candidates else None
        migration = Migration(legacy_dirs() + args.source_data, source, args.meeting_dir).plan()
        print(f"配置: {migration.config}\n数据: {migration.target}")
        print(f"待迁移来源 {len(migration.sources) - len(migration.manifests)} 个；更新采样清单 {len(migration.manifests)} 个")
        if args.apply:
            backup = migration.apply()
            print(f"迁移完成；备份: {backup}" if backup else "无需迁移")
        else:
            print("检查通过；加 --apply 执行（停止其他会议脚本后操作）")
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
