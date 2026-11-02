import copy
import unittest
from unittest import mock

from lib.process.promotion import prepared
from lib.refusal import Refusal


class Outputs(unittest.TestCase):
    def test_smoke_requires_every_built_binary_and_actual_version_help_surfaces(self):
        target = next(row for row in prepared.BUILD["targets"] if row["name"] == "windows")
        document = {"context": {"marker": "v1.2.3"}, "entries": {"smoke-windows": {"key": "a" * 64}, "bind-windows": {"key": "b" * 64}}}
        rows = [{"expect": name + " v1.2.3", "file": name + "-" + target["target"] + ".exe", "surfaces": {"--version": name + " v1.2.3", "--help": "usage"}} for name in ("demo", "demo-api")]
        loaded = {"basis": {"entry": {"kind": "binary-smoke", "binary": "b" * 64}}, "files": {"receipt.json": b"receipt"}, "receipt": {"action": "ship.binary.smoke", "marker": "v1.2.3", "binaries": rows}}
        with mock.patch.object(prepared.storage, "read", return_value=loaded), mock.patch.object(prepared.storage, "producer") as producer, mock.patch.object(prepared.storage, "summary", return_value={"verified": True}):
            self.assertEqual(prepared.smoke(object(), document, ["demo", "demo-api"], target), {"verified": True})
            producer.assert_called_once()
        alternatives = []
        for field, value in (("--version", "demo v1.2.2"), ("--help", "")):
            changed = copy.deepcopy(loaded)
            changed["receipt"]["binaries"][0]["surfaces"][field] = value
            alternatives.append(changed)
        changed = copy.deepcopy(loaded)
        changed["receipt"]["binaries"].pop()
        alternatives.append(changed)
        changed = copy.deepcopy(loaded)
        changed["basis"]["entry"]["binary"] = "c" * 64
        alternatives.append(changed)
        for value in alternatives:
            with self.subTest(value=value), mock.patch.object(prepared.storage, "read", return_value=value), mock.patch.object(prepared.storage, "producer"), self.assertRaises(Refusal):
                prepared.smoke(object(), document, ["demo", "demo-api"], target)

    def test_extra_publisher_or_missing_target_entry_refuses_before_byte_restoration(self):
        model = {"targets": {target["target"]: {"built": ["demo"], "installed": ["demo"]} for target in prepared.BUILD["targets"]}}
        expected = {prefix + target["name"]: {"key": "a" * 64} for target in prepared.BUILD["targets"] for prefix in ("dependencies-", "binary-", "bind-", "smoke-")} | {"validate": {"key": "b" * 64}}
        missing = dict(expected)
        missing.pop("smoke-macos")
        for entries in (missing, expected | {"release": {"decision": "run"}}):
            with mock.patch.object(prepared.chain, "frozen", return_value=model), mock.patch.object(prepared.storage, "read") as read, self.assertRaises(Refusal):
                prepared.complete(object(), {"entries": entries}, {})
            read.assert_not_called()

    def test_validator_reproduces_exact_configured_commands_and_original_producer(self):
        target = prepared.BUILD["targets"][0]
        model = {"targets": {target["target"]: {"built": ["demo"], "installed": ["demo"]}}}
        entries = {prefix + target["name"]: {"key": "a" * 64} for prefix in ("dependencies-", "binary-", "bind-", "smoke-")} | {"validate": {"key": "b" * 64}}
        source = {"repository": "PerishLab/demo"}
        loaded = {"basis": {"entry": {"kind": "binary-validate", "binary": "a" * 64}}, "files": {"receipt.json": b"receipt"}, "receipt": {"action": "ship.binary.configured", "steps": ["probe {marker}"]}}
        facts = {"build": {}, "binding": {}}
        with mock.patch.object(prepared.chain, "frozen", return_value=model), mock.patch.object(prepared.verification, "chain", return_value=facts), mock.patch.object(prepared.inventory, "planned"), mock.patch.object(prepared, "smoke", return_value={}), mock.patch.object(prepared.storage, "summary", return_value={}), mock.patch.object(prepared.storage, "read", return_value=loaded), mock.patch.object(prepared.storage, "producer") as producer, mock.patch.object(prepared.resources, "read_json", return_value={"PerishLab/demo": [["probe", "{marker}"]]}):
            prepared.complete(object(), {"entries": entries, "context": {}}, source)
            producer.assert_called_once()
            loaded["receipt"]["steps"] = []
            with self.assertRaises(Refusal):
                prepared.complete(object(), {"entries": entries, "context": {}}, source)
