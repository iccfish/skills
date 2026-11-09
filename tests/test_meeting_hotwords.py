"""Offline hotword persistence and request-contract tests; no real credentials or audio."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "meeting-transcribe" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import bailian
import config
import hotwords
import localio
import transcribe


class HotwordTests(unittest.TestCase):
    """Isolate configuration and fail any unexpected cloud or tool preparation."""

    def setUp(self):
        """Give each test a private configuration root with no inherited credentials."""
        temp = tempfile.TemporaryDirectory(prefix="hotword-tests-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        environment = patch.dict(os.environ, {"AGENT_SKILLS_CONFIG_DIR": str(self.root),
                                              "USERPROFILE": str(self.root), "HOME": str(self.root)}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.path = self.root / "meeting-transcribe.json"
        for name in ("_request",):
            guard = patch.object(bailian, name, side_effect=AssertionError("unexpected network"))
            guard.start()
            self.addCleanup(guard.stop)
        for name in ("ensure_tools", "install_deps", "resolve_vp_switch"):
            guard = patch.object(transcribe, name, side_effect=AssertionError("unexpected preparation"))
            guard.start()
            self.addCleanup(guard.stop)

    def write(self, path, data):
        """Write only synthetic local configuration."""
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def run_cli(self, args):
        """Capture CLI output without interpreting a printed plan as execution."""
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            code = transcribe.main(args)
        return code, output.getvalue()

    def test_crud_roundtrip_defaults_and_unrelated_fields(self):
        """CRUD changes persist, preserve other settings, and default additions to one."""
        self.write(self.path, {"api_key": "test-only", "custom": {"keep": True}})
        for args in (["--hotword-add", "示例产品"],
                     ["--hotword-add", "示例缩写", "--hotword-weight", "3"],
                     ["--hotword-update", "示例产品", "--hotword-weight", "5"],
                     ["--hotword-update", "示例产品", "--hotword-rename", "新示例产品"]):
            code, output = self.run_cli(args)
            self.assertEqual(code, 0, output)
            self.assertNotIn("test-only", output)
        self.assertEqual(config.load_config().hotwords, {"示例缩写": 3, "新示例产品": 5})
        code, output = self.run_cli(["--hotword-query", "新示例产品"])
        self.assertEqual((code, json.loads(output)["weight"]), (0, 5))
        self.assertEqual(self.run_cli(["--hotword-remove", "示例缩写"])[0], 0)
        code, output = self.run_cli(["--hotword-list"])
        self.assertEqual(json.loads(output)["hotwords"], {"新示例产品": 5})
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["api_key"], "test-only")
        self.assertEqual(stored["custom"], {"keep": True})

    def test_default_weight_is_one_and_empty_reads_do_not_create_files(self):
        """Reading empty settings stays read-only; omitted add weight is persisted as one."""
        self.assertEqual(self.run_cli(["--hotword-list"])[0], 0)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.run_cli(["--hotword-query", "缺失"])[0], 2)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.run_cli(["--hotword-add", "示例"])[0], 0)
        self.assertEqual(config.load_config().hotwords, {"示例": 1})

    def test_request_receives_exact_configured_vocabulary(self):
        """Verify the real serialized POST from loaded config, not just a helper's return."""
        self.run_cli(["--hotword-add", "示例产品"])
        self.run_cli(["--hotword-add", "示例专名", "--hotword-weight", "50"])
        cfg = config.load_config()
        with patch.object(bailian, "_request", return_value={"output": {"task_id": "test-task"}}) as request:
            bailian.submit(cfg, "https://audio.example.com/example.mp3")
        args = request.call_args.args
        self.assertEqual(args[0], "POST")
        body = json.loads(json.dumps(args[3], ensure_ascii=False))
        self.assertEqual(body["parameters"]["vocabulary"], {"示例产品": 1, "示例专名": 50})
        self.assertEqual(body["parameters"]["language_hints"], ["zh", "en"])
        self.assertTrue(body["parameters"]["diarization_enabled"])

    def test_empty_vocabulary_preserves_previous_request(self):
        """Old configurations neither require a new field nor send an empty API parameter."""
        cfg = config.load_config()
        with patch.object(bailian, "_request", return_value={"output": {"task_id": "test-task"}}) as request:
            bailian.submit(cfg, "https://audio.example.com/example.mp3", diarization=False)
        body = request.call_args.args[3]
        self.assertNotIn("vocabulary", body["parameters"])
        self.assertNotIn("diarization_enabled", body["parameters"])

    def test_invalid_vocabulary_never_submits(self):
        """Reject unsupported API weights even when a caller mutates an already loaded config."""
        cfg = config.Config(hotwords={"示例": 6})
        with self.assertRaises(ValueError):
            bailian.submit(cfg, "https://audio.example.com/example.mp3")

    def test_duplicate_missing_and_rename_conflicts_leave_bytes_unchanged(self):
        """Invalid CRUD never silently overwrites another term or partially saves."""
        self.write(self.path, {"hotwords": {"示例甲": 1, "示例乙": 3}})
        original = self.path.read_bytes()
        cases = (["--hotword-add", "示例甲"], ["--hotword-remove", "不存在"],
                 ["--hotword-update", "不存在", "--hotword-weight", "2"],
                 ["--hotword-update", "示例甲", "--hotword-rename", "示例乙"],
                 ["--hotword-update", "示例甲"], ["--hotword-add", "   "],
                 ["--hotword-add", "含\n换行"], ["--hotword-add", "新词", "--hotword-weight", "6"])
        for args in cases:
            with self.subTest(args=args):
                self.assertEqual(self.run_cli(args)[0], 2)
                self.assertEqual(self.path.read_bytes(), original)

    def test_strict_weights_terms_and_api_limits(self):
        """Reject bool/float/string weights and limits before the service randomly drops terms."""
        for value in (True, False, 0, 6, 49, 51, 1.0, "1", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                hotwords.normalize({"示例": value})
        for value in (None, [], {"": 1}, {"示例": 1, " 示例 ": 2}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                hotwords.normalize(value)
        self.assertEqual(len(hotwords.normalize({str(i): 1 for i in range(2000)})), 2000)
        with self.assertRaises(ValueError):
            hotwords.normalize({str(i): 1 for i in range(2001)})
        self.assertEqual(len(hotwords.normalize({str(i): 50 for i in range(50)})), 50)
        with self.assertRaises(ValueError):
            hotwords.normalize({str(i): 50 for i in range(51)})

    def test_explicit_and_environment_targets_match_transcription_precedence(self):
        """Inherit only vocabulary and never copy default credentials to an explicit target."""
        self.write(self.path, {"api_key": "test-default-only", "hotwords": {"默认词": 2}})
        target = self.root / "other.json"
        self.assertEqual(self.run_cli(["--config", str(target), "--hotword-add", "新词"])[0], 0)
        document = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(document, {"hotwords": {"默认词": 2, "新词": 1}})
        self.assertEqual(config.load_config(str(target)).hotwords, document["hotwords"])
        self.assertEqual(config.load_config().hotwords, {"默认词": 2})
        with patch.dict(os.environ, {"MEETING_TRANSCRIBE_CONFIG": str(target)}):
            self.assertEqual(self.run_cli(["--hotword-remove", "默认词"])[0], 0)
            self.assertEqual(config.load_config().hotwords, {"新词": 1})
        self.write(target, {"hotwords": {}})
        self.assertEqual(config.load_config(str(target)).hotwords, {})

    def test_corrupt_file_and_concurrent_writer_are_preserved(self):
        """Never replace malformed settings or another writer's update with a stale document."""
        self.path.write_bytes(b'{"hotwords":')
        self.assertEqual(self.run_cli(["--hotword-add", "示例"])[0], 2)
        self.assertEqual(self.path.read_bytes(), b'{"hotwords":')
        self.write(self.path, {"hotwords": {}})
        original_save = hotwords.save_document

        def concurrent_save(path, document):
            """Simulate another config update after the CRUD read but before saving."""
            self.write(path, {"hotwords": {}, "concurrent": True})
            return original_save(path, document)

        with patch.object(hotwords, "save_document", side_effect=concurrent_save):
            self.assertEqual(self.run_cli(["--hotword-add", "示例"])[0], 2)
        self.assertEqual(json.loads(self.path.read_text()), {"hotwords": {}, "concurrent": True})

    def test_management_rejects_mixed_actions_and_orphan_options(self):
        """Miscombined commands must fail rather than quietly skip a requested operation."""
        cases = (["--hotword-list", "audio.mp3"], ["--hotword-add", "示例", "--install-deps"],
                 ["--hotword-list", "--check-env"], ["--hotword-weight", "2"],
                 ["--hotword-list", "--hotword-weight", "2"],
                 ["--hotword-add", "示例", "--hotword-rename", "新词"],
                 ["--hotword-list", "--hotword-query", "示例"])
        for args in cases:
            with self.subTest(args=args), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                transcribe.main(args)
            self.assertEqual(raised.exception.code, 2)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
