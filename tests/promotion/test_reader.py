import copy
import json
import os
import unittest
from unittest import mock

from lib.content import canonical
from lib.identity.bind.spec import entry
from lib.process.promotion import reader
from lib.refusal import Refusal


class Reader(unittest.TestCase):
    def report(self):
        return {"schema": "plumb.windtun.qualification/v1", "subject": {"repository": "PerishLab/plumb", "marker": "v1.2.3-rc.1"}, "state": "qualified", "qualified": True, "platform": "linux-x86_64", "association": None, "promotion": False, "evidence": {}, "reason": None}

    def test_selection_budget_is_explicit_and_does_not_expand_existing_callers(self):
        with mock.patch.object(reader, "execute", return_value=(0, {})) as execute:
            reader.select(["release", "inspect"])
            execute.assert_called_once_with(["release", "inspect"], 2700, True)
            execute.reset_mock()
            for budget in (901, 2700, True, 0):
                with self.subTest(budget=budget), self.assertRaises(Refusal):
                    reader.capture([], budget)
            execute.assert_not_called()
            reader.capture([], 900)
            execute.assert_called_once_with([], 900)

    def test_only_complete_qualified_subject_can_supply_public_facts(self):
        original = self.report()
        with mock.patch.object(reader, "identity", return_value={}), mock.patch.object(reader, "capture", return_value=(0, original)):
            self.assertEqual(reader.check("PerishLab/plumb", "v1.2.3-rc.1")["report"], original)
        for field, value in (("subject", {}), ("association", True), ("promotion", True), ("qualified", 1), ("platform", "windows")):
            report = dict(original, **{field: value})
            with self.subTest(field=field), mock.patch.object(reader, "identity", return_value={}), mock.patch.object(reader, "capture", return_value=(0, report)), self.assertRaises(Refusal):
                reader.check("PerishLab/plumb", "v1.2.3-rc.1")
        report = dict(original, state="unknown", qualified=False, reason="unavailable")
        with mock.patch.object(reader, "identity", return_value={}), mock.patch.object(reader, "capture", return_value=(3, report)), self.assertRaises(OSError):
            reader.check("PerishLab/plumb", "v1.2.3-rc.1")

    def test_actual_reader_authority_must_match_verified_current_stable_seal(self):
        authority = {"producer": "v1.0.0@" + "a" * 40, "depot": "b" * 64}
        seal = {"product": "plumb", "channel": "stable", "releaseVersion": "v1.0.0", "commit": "a" * 40, "guard": authority}
        raw = canonical.encode(seal)
        pointer = {**seal, "seal": {"url": reader.AUTHORITY + "/v1/releases/stable/v1.0.0/seal.json", **entry(raw)}}
        with mock.patch.object(reader.release, "fetch", side_effect=lambda url: raw if url.endswith("seal.json") else canonical.encode(pointer)), mock.patch.object(reader, "capture", return_value=(0, authority)):
            self.assertEqual(reader.identity(), authority)
        with mock.patch.object(reader.release, "fetch", side_effect=lambda url: raw if url.endswith("seal.json") else canonical.encode(pointer)), mock.patch.object(reader, "capture", return_value=(0, dict(authority, producer="foreign"))), self.assertRaises(Refusal):
            reader.identity()

    def test_subprocess_withholds_storage_credentials_and_reaps_its_process_group(self):
        child = mock.Mock(pid=999, returncode=0)
        child.poll.return_value = 0
        observed = {}

        def spawn(argv, **options):
            observed.update(options)
            options["stdout"].write(b'{"ok":true}')
            options["stdout"].flush()
            return child

        with mock.patch.dict(os.environ, {"WHARF_R2_SECRET_ACCESS_KEY": "fixture-only", "GH_TOKEN": "reader-only"}), mock.patch.object(reader.subprocess, "Popen", side_effect=spawn), mock.patch.object(reader.os, "killpg") as killed:
            self.assertEqual(reader.capture(["release", "authority", "--json"]), (0, {"ok": True}))
        self.assertNotIn("WHARF_R2_SECRET_ACCESS_KEY", observed["env"])
        self.assertEqual(observed["env"]["GH_TOKEN"], "reader-only")
        self.assertTrue(observed["start_new_session"])
        killed.assert_called_once()
        child.wait.assert_called_once()

    def test_timeout_or_output_exhaustion_cannot_return_a_qualification(self):
        child = mock.Mock(pid=999)
        child.poll.return_value = None
        with mock.patch.object(reader.subprocess, "Popen", return_value=child), mock.patch.object(reader.time, "monotonic", side_effect=[0, 181]), mock.patch.object(reader.os, "killpg") as killed, self.assertRaises(Refusal):
            reader.capture(["windtun", "check"])
        killed.assert_called_once()
        child.wait.assert_called_once()

    def test_unrelated_verifier_main_provenance_does_not_change_required_facts(self):
        qualified = {"reader": {}, "report": {"evidence": {"credential": {}, "intent": {}, "source": {}, "tag": {}, "observer": {}, "world": {}, "final_requirements": {"requirements": {"policy": "current"}, "commit": "a" * 40}}}}
        changed = copy.deepcopy(qualified)
        changed["report"]["evidence"]["final_requirements"]["commit"] = "b" * 40
        self.assertEqual(reader.pins(qualified), reader.pins(changed))
