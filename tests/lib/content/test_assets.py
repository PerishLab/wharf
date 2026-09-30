import os
import tempfile
import unittest
from pathlib import Path

from lib.content import canonical
from lib.content.static import assets
from lib.refusal import Refusal
from tests.lib.content.test_preview import intent

FILES = {
    "index.html": b'<html><head><link rel="stylesheet" href="style.css"></head><body><img src="mark.svg"></body></html>',
    "style.css": b"body { color: black; }", "mark.svg": b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0L1 1"/></svg>',
}


class StaticAssets(unittest.TestCase):
    def test_safe_static_set_and_manifest(self):
        self.assertEqual(assets.vetted(FILES), FILES)
        document = assets.manifest(FILES, intent())
        self.assertEqual(document["source"]["commit"], intent()["source"]["commit"])
        claim = {key: value for key, value in document.items() if key != "digest"}
        self.assertEqual(document["digest"], canonical.digest(claim))
        self.assertEqual([item["path"] for item in document["files"]], sorted(FILES))
        self.assertIn(b"script-src 'none'", assets.HEADERS)
        self.assertIn(b"Cache-Control: no-store", assets.HEADERS)

    def test_identity_depends_on_bytes(self):
        before = assets.manifest(FILES, intent())
        after = assets.manifest(dict(FILES, **{"style.css": b"body { color: blue; }"}), intent())
        self.assertNotEqual(before["digest"], after["digest"])

    def test_active_html_refuses(self):
        values = (b"<script>alert(1)</script>", b'<img onerror="x">', b'<iframe src="x"></iframe>', b'<meta http-equiv="refresh" content="0;url=https://x">', b'<a href="javascript:alert(1)">x</a>', b'<link rel="preload" href="x">', b'<div class="a" class="b">')
        for body in values:
            with self.subTest(body=body), self.assertRaises(Refusal):
                assets.validate("index.html", body)

    def test_external_and_escaped_css_refuses(self):
        for body in (b"@import 'x.css';", b"x{background:url(https://example.com/x)}", b"x{color:e\\78pression(foo)}", b"x{behavior:url(x)}"):
            with self.subTest(body=body), self.assertRaises(Refusal):
                assets.validate("style.css", body)

    def test_active_svg_refuses(self):
        values = (b'<svg><script>run()</script></svg>', b'<svg onload="x"/>', b'<svg><foreignObject/></svg>', b'<svg><use href="https://example.com/x"/></svg>', b'<!DOCTYPE svg [<!ENTITY x "y">]><svg/>', b'<svg xml:base="https://example.com/"/>')
        for body in values:
            with self.subTest(body=body), self.assertRaises(Refusal):
                assets.validate("mark.svg", body)

    def test_missing_and_escaping_references_refuse(self):
        for body in (b'<img src="missing.png">', b'<img src="../outside.png">', b'<img src="%2e%2e/outside.png">', b'<img src="//example.com/x">'):
            with self.subTest(body=body), self.assertRaises(Refusal):
                assets.vetted({"index.html": body})

    def test_private_reserved_and_unsupported_files_refuse(self):
        for name in (".env", "private/a.svg", "credentials.svg", "_headers", "_redirects", assets.IDENTITY, "worker.js", "key.pem"):
            with self.subTest(name=name), self.assertRaises(Refusal):
                assets.vetted(dict(FILES, **{name: b"data"}))

    def test_size_and_file_budgets(self):
        for files in ({}, {"index.html": b""}, {"index.html": b"x" * (assets.MAX_FILE + 1)}, {f"{number}.css": b"x" for number in range(1001)}):
            with self.subTest(count=len(files)), self.assertRaises(Refusal):
                assets.vetted(files)

    def test_binary_signature_and_private_key(self):
        with self.assertRaises(Refusal):
            assets.validate("mark.png", b"HTML disguised as an image")
        with self.assertRaises(Refusal):
            assets.validate("index.html", b"-----BEGIN PRIVATE KEY-----")
        self.assertEqual(assets.validate("mark.png", b"\x89PNG\r\n\x1a\nbytes"), [])

    def test_collect_rejects_symlinks_hardlinks_and_fifos(self):
        for kind in ("symlink", "hardlink", "fifo"):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "index.html").write_bytes(b"<html></html>")
                path = root / "mark.css"
                if kind == "symlink":
                    path.symlink_to("index.html")
                elif kind == "hardlink":
                    os.link(root / "index.html", path)
                else:
                    os.mkfifo(path)
                with self.subTest(kind=kind), self.assertRaises(Refusal):
                    assets.collect(root)

    def test_collect_nested_files_and_root_relative_references(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "nested").mkdir()
            (root / "index.html").write_bytes(b'<img src="/nested/mark.svg">')
            (root / "nested/mark.svg").write_bytes(FILES["mark.svg"])
            self.assertEqual(set(assets.collect(root)), {"index.html", "nested/mark.svg"})


if __name__ == "__main__":
    unittest.main()
