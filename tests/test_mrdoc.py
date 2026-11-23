"""Offline upload-path tests for the mrdoc client; no network, credentials or real site."""
from contextlib import redirect_stdout
import io
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "mrdoc" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import mrdoc


class MediaUrlTests(unittest.TestCase):
    """Verify the absolute-address normalization shared by both upload commands."""

    def setUp(self):
        """Pin the origin so assertions do not depend on any local configuration."""
        self._base = mrdoc.QBASE.get("base")
        mrdoc.QBASE["base"] = "https://notes.example.com"
        self.addCleanup(self._restore)

    def _restore(self):
        """Restore the module-level base after each test."""
        mrdoc.QBASE["base"] = self._base

    def test_attachment_relative_name_gets_media_prefix(self):
        """upload_file returns a MEDIA_ROOT-relative name, which needs /media prepended."""
        self.assertEqual(
            mrdoc.media_url("attachment/2026/11/report.pdf"),
            "https://notes.example.com/media/attachment/2026/11/report.pdf",
        )
        self.assertEqual(
            mrdoc.media_url("/attachment/2026/11/report.pdf"),
            "https://notes.example.com/media/attachment/2026/11/report.pdf",
        )

    def test_image_path_with_media_prefix_is_not_doubled(self):
        """upload_img already returns /media/..., so the prefix must not be added twice."""
        self.assertEqual(
            mrdoc.media_url("/media/202611/pic.jpg"),
            "https://notes.example.com/media/202611/pic.jpg",
        )
        self.assertEqual(
            mrdoc.media_url("media/202611/pic.jpg"),
            "https://notes.example.com/media/202611/pic.jpg",
        )

    def test_absolute_url_passes_through(self):
        """An already absolute address must be returned untouched."""
        self.assertEqual(mrdoc.media_url("http://cdn.example.com/a.png"), "http://cdn.example.com/a.png")

    def test_missing_trailing_slash_in_base(self):
        """A base configured without a trailing slash must not produce a doubled slash."""
        mrdoc.QBASE["base"] = "https://notes.example.com/"
        self.assertEqual(
            mrdoc.media_url("attachment/x.pdf"),
            "https://notes.example.com/media/attachment/x.pdf",
        )


class UploadCommandTests(unittest.TestCase):
    """Check the two upload commands print absolute addresses and use the right form field."""

    def setUp(self):
        """Provide a temporary file and a pinned origin for every command test."""
        temp = tempfile.TemporaryDirectory(prefix="mrdoc-upload-tests-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self._base = mrdoc.QBASE.get("base")
        self._json = mrdoc.QBASE.get("json")
        mrdoc.QBASE["base"] = "https://notes.example.com"
        mrdoc.QBASE["json"] = False
        self.addCleanup(self._restore)

    def _restore(self):
        """Restore module state so later tests are unaffected."""
        mrdoc.QBASE["base"] = self._base
        mrdoc.QBASE["json"] = self._json

    def _file(self, name, payload=b"payload"):
        """Create a temp file with the given name and return its path."""
        path = self.root / name
        path.write_bytes(payload)
        return str(path)

    def test_attach_prints_media_absolute_url(self):
        """attach must resolve the returned relative name into an openable /media address."""
        response = {"status": True, "data": {"name": "report.pdf", "url": "attachment/2026/11/report.pdf"}}
        captured = {}

        def fake_request(path, **kw):
            """Capture the call arguments and answer with a canned upload result."""
            captured["path"] = path
            captured.update(kw)
            return response

        out = io.StringIO()
        with patch.object(mrdoc, "_request", side_effect=fake_request), redirect_stdout(out):
            mrdoc.cmd_attach(SimpleNamespace(file=self._file("report.pdf")))
        text = out.getvalue()
        self.assertEqual(captured["path"], "upload_file/")
        self.assertIn("attachment_upload", captured["files"])
        self.assertIn("https://notes.example.com/media/attachment/2026/11/report.pdf", text)

    def test_attach_reports_server_rejection(self):
        """A rejected suffix must surface the server reason instead of a bare exit."""
        with patch.object(mrdoc, "_request", return_value={"status": False, "data": "不支持的格式"}):
            with self.assertRaises(SystemExit) as ctx:
                mrdoc.cmd_attach(SimpleNamespace(file=self._file("note.md")))
        self.assertIn("不支持的格式", str(ctx.exception))
        self.assertIn("站点未放行该后缀", str(ctx.exception))

    def test_upload_prints_absolute_markdown(self):
        """upload must emit an absolute image URL usable outside the site."""
        with patch.object(mrdoc, "_request", return_value={"success": 1, "url": "/media/202611/pic.jpg"}):
            out = io.StringIO()
            with redirect_stdout(out):
                mrdoc.cmd_upload(SimpleNamespace(file=self._file("pic.jpg")))
        text = out.getvalue()
        self.assertIn("https://notes.example.com/media/202611/pic.jpg", text)
        self.assertIn("![](https://notes.example.com/media/202611/pic.jpg)", text)


if __name__ == "__main__":
    unittest.main()
