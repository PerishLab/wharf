import copy
import json
import tempfile
import unittest
from pathlib import Path

from lib.content import canonical
from lib.content.static import assets
from lib.refusal import Refusal
from lib.store import handoff, workload
from tests.lib.content.test_assets import FILES
from tests.lib.content.test_preview import intent
from tests.lib.content.test_static import guarded
from tests.lib.store.memory import Memory


def receipt():
    request = intent()
    tools = {name: {"path": f"/tools/{name}", "sha256": "7" * 64, "version": "1.0.0"} for name in handoff.evidence.TOOLS}
    tools["plumb"]["version"] = "plumb v0.66.0"
    return {
        "schema": "wharf.preview.build/v1", "source": dict(request["source"], repository=request["repository"]), "app": request["app"],
        "guard": guarded(request, Path("/source")), "tools": tools, "implementation": handoff.world(),
    }


class Handoff(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp()) / "bundle"
        self.receipt = receipt()
        self.document = handoff.pack(self.directory, FILES, self.receipt)

    def test_local_roundtrip_and_fixed_policy(self):
        self.assertEqual(handoff.local(self.directory), self.document)
        payload = handoff.verify(self.document, lambda key: (self.directory / key).read_bytes())
        self.assertEqual(payload["_headers"], assets.HEADERS)
        manifest = json.loads(payload[assets.IDENTITY])
        self.assertEqual(manifest["digest"], self.document["content"])
        self.assertEqual(manifest["source"], self.receipt["source"])

    def test_immutable_workload_roundtrip_and_retry(self):
        bucket = Memory()
        first = handoff.publish(bucket, self.directory)
        second = handoff.publish(bucket, self.directory)
        self.assertEqual((first["state"], second["state"]), ("recorded", "already-recorded"))
        document, files = handoff.fetch(bucket, self.document["key"])
        self.assertEqual(document, self.document)
        self.assertEqual({name: body for name, body in files.items() if name in FILES}, FILES)

    def test_changed_local_blob_refuses_before_publish(self):
        name = self.document["files"]["index.html"]["blob"]
        (self.directory / name).write_bytes(b"different")
        bucket = Memory()
        with self.assertRaises(Refusal):
            handoff.publish(bucket, self.directory)
        self.assertEqual(bucket.writes, [])

    def test_extra_local_payload_and_index_symlink_refuse(self):
        (self.directory / "unselected").write_bytes(b"data")
        with self.assertRaises(Refusal):
            handoff.local(self.directory)
        (self.directory / "unselected").unlink()
        index = self.directory / "bundle.json"
        index.rename(self.directory / "other")
        index.symlink_to("other")
        with self.assertRaises(Refusal):
            handoff.local(self.directory)

    def test_remote_blob_and_record_tamper_refuse(self):
        bucket = Memory()
        handoff.publish(bucket, self.directory)
        key = self.document["key"]
        prefix = workload.prefix(key)
        name = self.document["files"]["index.html"]["blob"]
        bucket.objects[prefix + "blobs/" + name] = b"changed"
        with self.assertRaises(Refusal):
            handoff.fetch(bucket, key)
        bucket = Memory()
        handoff.publish(bucket, self.directory)
        record = json.loads(bucket.get(prefix + "record.json"))
        record["files"][name]["sha256"] = "0" * 64
        bucket.objects[prefix + "record.json"] = canonical.encode(record)
        with self.assertRaises(Refusal):
            handoff.fetch(bucket, key)

    def test_manifest_policy_and_key_tamper_refuse(self):
        for field in ("key", "policy", "content"):
            document = copy.deepcopy(self.document)
            if field == "key":
                document[field] = "0" * 64
            elif field == "content":
                document[field] = "0" * 64
            else:
                document["files"]["_headers"]["blob"] = "0" * 64
            with self.subTest(field=field), self.assertRaises((Refusal, FileNotFoundError)):
                handoff.verify(document, lambda key: (self.directory / key).read_bytes())

    def test_path_traversal_in_bundle_refuses(self):
        document = copy.deepcopy(self.document)
        document["files"]["../private"] = document["files"]["index.html"]
        with self.assertRaises(Refusal):
            handoff.verify(document, lambda key: (self.directory / key).read_bytes())

    def test_content_key_has_no_environment_or_request_context(self):
        self.assertNotIn("request", self.document["basis"]["receipt"])
        self.assertNotIn("name", self.document["basis"]["receipt"])
        self.assertEqual(handoff.describe(FILES, self.receipt)[0]["key"], self.document["key"])
        modified = copy.deepcopy(self.receipt)
        modified["tools"]["node"]["sha256"] = "8" * 64
        self.assertNotEqual(handoff.describe(FILES, modified)[0]["key"], self.document["key"])

    def test_source_guard_mismatch_refuses(self):
        modified = copy.deepcopy(self.receipt)
        modified["source"]["commit"] = "9" * 40
        with self.assertRaises(Refusal):
            handoff.describe(FILES, modified)

    def test_remote_index_bytes_and_basis_tamper_refuse(self):
        for name in ("blobs/bundle.json", "basis.json"):
            bucket = Memory()
            handoff.publish(bucket, self.directory)
            prefix = workload.prefix(self.document["key"])
            bucket.objects[prefix + name] += b" "
            with self.subTest(name=name), self.assertRaises(Refusal):
                handoff.fetch(bucket, self.document["key"])

    def test_same_inputs_different_output_refuses_as_nondeterminism(self):
        bucket = Memory()
        handoff.publish(bucket, self.directory)
        alternate = Path(tempfile.mkdtemp()) / "bundle"
        changed = dict(FILES, **{"style.css": b"body {color: blue;}"})
        document = handoff.pack(alternate, changed, self.receipt)
        self.assertEqual(document["key"], self.document["key"])
        self.assertNotEqual(document["content"], self.document["content"])
        with self.assertRaises(Refusal):
            handoff.publish(bucket, alternate)

    def test_changed_implementation_receipt_refuses(self):
        modified = copy.deepcopy(self.receipt)
        modified["implementation"]["lib.media.node"] = "0" * 64
        with self.assertRaises(Refusal):
            handoff.describe(FILES, modified)


if __name__ == "__main__":
    unittest.main()
