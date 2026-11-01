import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.process.association import signature
from lib.process.association.spec import sha
from lib.refusal import Refusal


class Signature(unittest.TestCase):
    def seat(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        tool = root / "rcodesign"
        tool.write_bytes(b"prepared tool")
        self.enterContext(mock.patch.dict(signature.POLICY["signer"], {"sha256": sha(tool.read_bytes())}))
        self.enterContext(mock.patch.object(signature.shutil, "which", return_value=str(tool)))
        return tool

    def test_child_holds_no_credentials_and_uses_exact_prepared_signer(self):
        tool = self.seat()
        calls = []

        def execute(argv, **settings):
            calls.append((argv, settings))
            Path(argv[-1]).write_bytes(b"signed")

        with mock.patch.object(signature.subprocess, "run", side_effect=execute):
            self.assertEqual(signature.sign(b"unbound", "demo"), b"signed")
        self.assertEqual(calls[0][0][:2], [str(tool), "sign"])
        self.assertEqual(set(calls[0][1]["env"]), {"HOME", "LANG"})
        self.assertEqual(calls[0][1]["stdin"], subprocess.DEVNULL)
        self.assertEqual(calls[0][1]["stdout"], subprocess.DEVNULL)
        self.assertEqual(calls[0][1]["stderr"], subprocess.DEVNULL)
        self.assertEqual(calls[0][1]["timeout"], 60)
        self.assertFalse(Path(calls[0][0][-1]).exists())

    def test_changed_or_missing_tool_refuses_without_invocation(self):
        tool = self.seat()
        tool.write_bytes(b"changed")
        with mock.patch.object(signature.subprocess, "run") as invoked, self.assertRaises(Refusal):
            signature.sign(b"unbound", "demo")
        invoked.assert_not_called()
        with mock.patch.object(signature.shutil, "which", return_value=None), self.assertRaises(Refusal):
            signature.sign(b"unbound", "demo")

    def test_timeout_or_changed_tool_after_execution_refuses(self):
        tool = self.seat()
        with mock.patch.object(signature.subprocess, "run", side_effect=subprocess.TimeoutExpired("sign", 60)), self.assertRaisesRegex(Refusal, "transformation failed"):
            signature.sign(b"unbound", "demo")

        def changed(argv, **settings):
            tool.write_bytes(b"substituted")

        with mock.patch.object(signature.subprocess, "run", side_effect=changed), self.assertRaises(Refusal):
            signature.sign(b"unbound", "demo")
