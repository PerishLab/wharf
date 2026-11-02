import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.content import canonical
from lib.process.promotion import prepared
from lib.refusal import Refusal
from lib.store import workload
from tests.lib.store.memory import Memory


class Material(unittest.TestCase):
    def publish(self, result):
        basis = {"schema": prepared.MATERIAL, "observer": result["observer"], "plan": result["plan"], "intent": result["intent"]}
        key = workload.key(basis, prepared.code())
        bucket = Memory()
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "prepared.json").write_bytes(canonical.encode(result))
            workload.publish(bucket, key, workload.Produced(Path(directory), basis, {"observer": result["observer"]}))
        return bucket, key, basis

    def test_independent_read_checks_complete_facts_record_and_bytes(self):
        result = {"schema": prepared.SCHEMA, "observer": {"run": "7"}, "plan": {"sha256": "a" * 64}, "intent": {"tag": "b" * 40}, "publication": False, "promotion": False}
        bucket, key, basis = self.publish(result)
        prepared.restore(bucket, key, (basis, result))
        before = list(bucket.writes)
        prefix = workload.prefix(key)
        record = json.loads(bucket.get(prefix + "record.json"))
        changed = copy.deepcopy(result)
        changed["promotion"] = True
        with self.assertRaises(Refusal):
            prepared.restore(bucket, key, (basis, changed))
        for alteration in ({"context": {"observer": {"run": "8"}}}, {"files": record["files"] | {"extra.json": {"size": 1, "sha256": "c" * 64}}}, {"hash_version": True}, {"key": "d" * 64}):
            bucket.objects[prefix + "record.json"] = canonical.encode(record | alteration)
            with self.subTest(alteration=alteration), self.assertRaises(Refusal):
                prepared.restore(bucket, key, (basis, result))
        bucket.objects[prefix + "record.json"] = canonical.encode(record)
        bucket.objects[prefix + "blobs/prepared.json"] = b"changed"
        with self.assertRaises(Refusal):
            prepared.restore(bucket, key, (basis, result))
        self.assertEqual(bucket.writes, before)

    def test_every_publication_role_must_have_been_skipped_without_steps(self):
        plan = {"name": "[plan] entries", "status": "completed", "conclusion": "success"}
        for name in sorted(prepared.FORBIDDEN | {"[publish] cargo", "[deploy] cfworker", "[point] channel"}):
            row = {"name": name, "status": "completed", "conclusion": "skipped"}
            others = [{"name": value, "status": "completed", "conclusion": "skipped"} for value in prepared.FORBIDDEN - {name}]
            prepared.jobs([plan, row, *others])
            for change in ({"conclusion": "success"}, {"status": "queued"}, {"steps": [{"name": "writer"}]}):
                with self.subTest(name=name, change=change), self.assertRaises(Refusal):
                    prepared.jobs([plan, row | change, *others])

    def test_publication_reservation_stops_before_restoring_artifacts(self):
        source = {"repository": "PerishLab/concord", "marker": "v0.58.0", "commit": "a" * 40, "tree": "b" * 40}
        observer = {"run": "7", "attempt": "1", "wharf": "c" * 40}
        held = source | {"observer": observer, "run": "7", "planned": "1", "preparation": "{}"}
        context = source | observer
        bucket = Memory()
        bucket.put(prepared.plan.publication(context), b"reserved")
        rows = [{"name": "[plan] entries", "status": "completed", "conclusion": "success"}] + [{"name": name, "status": "completed", "conclusion": "skipped"} for name in prepared.FORBIDDEN]
        with mock.patch.object(prepared.preflight, "active", return_value=(observer, rows)), mock.patch.object(prepared.preflight, "event", return_value=source | {"prepare": True}), mock.patch.object(prepared.preflight, "confirm"), mock.patch.object(prepared.r2, "configured", return_value=bucket), mock.patch.object(prepared.verification, "planned", return_value=({"context": context}, observer)), mock.patch.object(prepared, "complete") as complete:
            with self.assertRaises(Refusal):
                prepared.assess(held)
            complete.assert_not_called()
