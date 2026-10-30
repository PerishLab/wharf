import base64
import copy
import hashlib
import json
import subprocess
import unittest
from unittest import mock

from lib.check import source
from lib.content import canonical
from lib.process.trial import context, policy
from lib.refusal import Refusal


def world(commit="1" * 40, requirements=None):
    raw = canonical.encode(requirements or policy.expected())
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    return {
        "": dict(policy.IDENTITY, owner=dict(policy.OWNER)),
        "branches/main": {"name": "main", "protected": True, "commit": {"sha": commit}},
        "git/commits/" + commit: {"sha": commit, "tree": {"sha": "2" * 40}},
        policy.CONTENTS + "?ref=" + commit: {"type": "file", "path": policy.CONTENTS.removeprefix("contents/"), "encoding": "base64", "size": len(raw), "sha": blob, "content": base64.encodebytes(raw).decode()},
    }


class Requirements(unittest.TestCase):
    def test_gate_uses_the_actual_validator_without_fingerprinting_itself(self):
        actual = context.fingerprint()
        self.assertNotIn("lib.process.trial.policy", actual)
        self.assertNotIn("lib.check.source", actual)
        self.assertNotIn("resource:" + policy.RESOURCE, actual)
        self.assertEqual(policy.expected(), {"schema": "wharf.windtun.requirements/v1", "policy": context.POLICY, "implementation": canonical.digest(actual)})
        with mock.patch.object(context, "fingerprint", return_value={"changed": "actual validator"}):
            with mock.patch.object(policy.resources, "read_bytes", return_value=canonical.encode(policy.expected())):
                self.assertEqual(source.check(".", []), [])
            with mock.patch.object(policy.resources, "read_bytes", return_value=canonical.encode(dict(policy.expected(), implementation="0" * 64))):
                self.assertTrue(source.check(".", []))

    def test_gate_refuses_missing_malformed_or_stale_derived_metadata(self):
        expected = policy.expected()
        bad = [b"", b"{}", b"{", json.dumps(expected, indent=2).encode(), canonical.encode(dict(expected, commit="1" * 40)), canonical.encode(dict(expected, implementation="0" * 64))]
        for raw in bad:
            with self.subTest(raw=raw), mock.patch.object(policy.resources, "read_bytes", return_value=raw):
                self.assertTrue(policy.findings())
        with mock.patch.object(policy.resources, "read_bytes", side_effect=OSError):
            self.assertTrue(policy.findings())

    def test_exact_protected_main_and_native_blob_are_read_twice(self):
        values = world()
        with mock.patch.object(policy, "api", side_effect=lambda path: copy.deepcopy(values[path])) as api:
            result = policy.current()
        self.assertEqual([call.args[0] for call in api.call_args_list], ["", "branches/main", "git/commits/" + "1" * 40, "contents/resources/check/windtun.json?ref=" + "1" * 40, "branches/main", ""])
        self.assertEqual(result["requirements"], policy.expected())
        self.assertEqual(result["deployment"]["commit"], "1" * 40)
        self.assertEqual(result["deployment"]["tree"], "2" * 40)
        self.assertEqual(result["deployment"]["blob"]["sha256"], hashlib.sha256(canonical.encode(policy.expected())).hexdigest())
        self.assertIs(result["promotion"], False)

    def test_unrelated_main_moves_keep_requirements_but_actual_changes_do_not(self):
        results = []
        for commit in ("1" * 40, "3" * 40):
            values = world(commit)
            with mock.patch.object(policy, "api", side_effect=lambda path: values[path]):
                results.append(policy.current())
        self.assertNotEqual(results[0]["deployment"]["commit"], results[1]["deployment"]["commit"])
        self.assertEqual(results[0]["requirements"], results[1]["requirements"])
        with mock.patch.object(context, "fingerprint", return_value={"new": "algorithm"}):
            changed = policy.expected()
        values = world("4" * 40, changed)
        with mock.patch.object(policy, "api", side_effect=lambda path: values[path]):
            self.assertEqual(policy.current()["requirements"], changed)
        self.assertNotEqual(results[0]["requirements"], changed)

    def test_fork_owner_branch_protection_and_commit_disagreement_refuse(self):
        changes = [("", "id", 1), ("", "node_id", "foreign"), ("", "full_name", "fork/wharf"), ("", "default_branch", "feature"), ("", "visibility", "private"), ("", "archived", True), ("", "owner", dict(policy.OWNER, id=1)), ("branches/main", "name", "feature"), ("branches/main", "protected", False), ("branches/main", "protected", 1), ("branches/main", "commit", {"sha": "../foreign"}), ("git/commits/" + "1" * 40, "sha", "3" * 40), ("git/commits/" + "1" * 40, "tree", {"sha": "invalid"})]
        for path, name, value in changes:
            with self.subTest(path=path, name=name):
                values = world()
                values[path][name] = value
                with mock.patch.object(policy, "api", side_effect=lambda path: values[path]), self.assertRaises(Refusal):
                    policy.current()

    def test_deployment_changes_during_readback_refuse(self):
        for changed in ("branches/main", ""):
            values = world()
            counts = {}

            def api(path):
                counts[path] = counts.get(path, 0) + 1
                value = copy.deepcopy(values[path])
                if path == changed and counts[path] == 2:
                    value.update({"commit": {"sha": "3" * 40}} if path else {"default_branch": "foreign"})
                return value

            with self.subTest(changed=changed), mock.patch.object(policy, "api", side_effect=api), self.assertRaises(Refusal):
                policy.current()

    def test_remote_content_identity_bounds_and_bytes_refuse(self):
        path = policy.CONTENTS + "?ref=" + "1" * 40
        changes = [("path", "foreign.json"), ("type", "symlink"), ("encoding", "none"), ("size", True), ("size", 4097), ("content", "A" * 6001), ("content", "?"), ("sha", "0" * 40), ("content", base64.b64encode(b"{}").decode())]
        for name, value in changes:
            with self.subTest(name=name, value=value):
                values = world()
                values[path][name] = value
                with mock.patch.object(policy, "api", side_effect=lambda path: values[path]), self.assertRaises(Refusal):
                    policy.current()

    def test_unknown_formats_and_pass_cannot_supply_the_current_expectation(self):
        good = policy.expected()
        values = [dict(good, schema="unknown"), dict(good, policy="foreign"), dict(good, implementation=True), dict(good, implementation="G" * 64), dict(good, observer={}), {"schema": "wharf.windtun.pass/v1", "policy": good["policy"], "implementation": good["implementation"]}]
        for value in values:
            entries = world(requirements=value)
            with self.subTest(value=value), mock.patch.object(policy, "api", side_effect=lambda path: entries[path]), self.assertRaises(Refusal):
                policy.current()

    def test_unavailable_or_malformed_provider_is_not_a_fallback(self):
        for error in (OSError(), subprocess.CalledProcessError(1, "gh"), ValueError()):
            with self.subTest(error=type(error).__name__), mock.patch.object(policy, "api", side_effect=error), self.assertRaises(Refusal):
                policy.current()

    def test_provider_hostname_and_repository_are_fixed(self):
        with mock.patch.object(policy.subprocess, "check_output", return_value=b"{}") as call:
            self.assertEqual(policy.api("branches/main"), {})
        self.assertEqual(call.call_args.args[0], ["gh", "api", "--hostname", "github.com", "repos/PerishLab/wharf/branches/main"])
