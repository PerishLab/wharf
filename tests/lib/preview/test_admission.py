import copy
import shutil
import unittest
from unittest import mock

from lib.content import canonical
from lib.content.static import admission, workspace
from lib.refusal import Refusal
from tests.lib.content.test_preview import intent, target
from tests.lib.content.test_static import StaticRepository

CONTEXT = {"run": 123, "attempt": 1, "commit": "1" * 40}


def request(operation="apply"):
    return dict(intent(operation), caller="alice", workflow="PerishLab/wharf/.github/workflows/preview.yml@" + CONTEXT["commit"])


class Provider:
    def __init__(self):
        control = {"id": 10, "full_name": "PerishLab/wharf"}
        self.observed = {"id": 123, "run_attempt": 1, "repository": control, "head_repository": dict(control), "event": "workflow_dispatch", "path": ".github/workflows/preview.yml", "head_branch": "main", "head_sha": CONTEXT["commit"], "status": "in_progress", "actor": {"id": 20, "login": "alice"}, "triggering_actor": {"id": 21, "login": "bob"}}
        self.permissions = {user: {"permission": "write", "user": {"id": identifier, "login": user, "permissions": {"push": True}}} for user, identifier in (("alice", 20), ("bob", 21))}
        self.source = {"sha": "d" * 40, "tree": {"sha": "e" * 40}}
        self.calls = []

    def repository(self, name):
        self.calls.append(("repository", name))
        return {"id": 10 if name == "PerishLab/wharf" else 11, "full_name": name, "archived": False, "default_branch": "main"}

    def run(self, identity):
        self.calls.append(("run", identity))
        return copy.deepcopy(self.observed)

    def permission(self, name, user):
        self.calls.append(("permission", name, user))
        return copy.deepcopy(self.permissions[user])

    def commit(self, name, identity):
        self.calls.append(("commit", name, identity))
        return copy.deepcopy(self.source)


def store(value=None):
    bucket = mock.Mock(spec=["get"])
    bucket.get.return_value = canonical.encode(target() if value is None else value)
    return bucket


class Admission(unittest.TestCase):
    def test_success_binds_observations_and_reads_only(self):
        api, bucket = Provider(), store()
        result = admission.admit(request(), bucket, CONTEXT, api)
        self.assertEqual(result["intent"], canonical.digest(request()))
        self.assertEqual(result["actors"]["triggering_actor"]["id"], 21)
        self.assertEqual(result["source"], {"commit": "d" * 40, "tree": "e" * 40})
        bucket.get.assert_called_once_with("preview/v1/registrations/PerishLab/crest/crest-review.json")

    def test_current_registration_failures_precede_provider_reads(self):
        for changed in ({"enabled": False}, {"generation": "9" * 32}, {"repository": "PerishLab/design"}, {"access": "private"}):
            api = Provider()
            with self.subTest(changed=changed), self.assertRaises(Refusal):
                admission.admit(request(), store(dict(target(), **changed)), CONTEXT, api)
            self.assertFalse(api.calls)
        for body in (None, b"{}", b"x" * 65537, b'{"a":1,"a":2}'):
            bucket = store()
            bucket.get.return_value = body
            with self.subTest(body=str(body)[:30]), self.assertRaises(Refusal):
                admission.admit(request(), bucket, CONTEXT, Provider())

    def test_run_identity_and_active_main_preview_only(self):
        changes = {"id": 124, "run_attempt": 2, "event": "pull_request", "path": ".github/workflows/ship.yml", "head_branch": "feature/68", "head_sha": "2" * 40, "status": "completed", "repository": {"id": 999, "full_name": "PerishLab/wharf"}, "head_repository": {"id": 10, "full_name": "attacker/wharf"}}
        for key, value in changes.items():
            api = Provider()
            api.observed[key] = value
            with self.subTest(key=key), self.assertRaises(Refusal):
                admission.admit(request(), store(), CONTEXT, api)

    def test_both_actual_actors_need_matching_write_permission(self):
        for user in ("alice", "bob"):
            for field, value in (("permission", "read"), ("user", {"id": 99, "login": user, "permissions": {"push": True}}), ("user", {"id": 20 if user == "alice" else 21, "login": user, "permissions": {"push": False}})):
                api = Provider()
                api.permissions[user][field] = value
                with self.subTest(user=user, field=field), self.assertRaises(Refusal):
                    admission.admit(request(), store(), CONTEXT, api)

    def test_request_authored_identity_cannot_replace_provider(self):
        for changed in ({"caller": "bob"}, {"workflow": "caller-supplied"}):
            with self.subTest(changed=changed), self.assertRaises(Refusal):
                admission.admit(dict(request(), **changed), store(), CONTEXT, Provider())

    def test_missing_provider_identities_and_unreadable_authority_refuse(self):
        for key in ("actor", "triggering_actor", "repository", "head_repository"):
            api = Provider()
            del api.observed[key]
            with self.subTest(key=key), self.assertRaises(Refusal):
                admission.admit(request(), store(), CONTEXT, api)
        bucket = store()
        bucket.get.side_effect = Refusal("unknown store read")
        with self.assertRaises(Refusal):
            admission.admit(request(), bucket, CONTEXT, Provider())
        for value in ({"id": 10, "full_name": "PerishLab/wharf", "archived": True}, {"id": 10, "full_name": "attacker/wharf", "archived": False}):
            api = Provider()
            api.repository = mock.Mock(return_value=value)
            with self.subTest(value=value), self.assertRaises(Refusal):
                admission.admit(request(), store(), CONTEXT, api)

    def test_missing_malformed_context_and_remote_source_refuse(self):
        for context in ({}, dict(CONTEXT, attempt=True), dict(CONTEXT, commit="bad")):
            with self.subTest(context=context), self.assertRaises(Refusal):
                admission.admit(request(), store(), context, Provider())
        for value in ({}, {"sha": "9" * 40, "tree": {"sha": "e" * 40}}, {"sha": "d" * 40, "tree": {"sha": "9" * 40}}):
            api = Provider()
            api.source = value
            with self.subTest(value=value), self.assertRaises(Refusal):
                admission.admit(request(), store(), CONTEXT, api)

    def test_inspect_discard_never_read_source(self):
        for operation in ("inspect", "discard"):
            api = Provider()
            result = admission.admit(request(operation), store(), CONTEXT, api)
            self.assertIsNone(result["source"])
            self.assertFalse(any(call[0] == "commit" for call in api.calls))
            with self.assertRaises(Refusal):
                admission.qualify("missing", request(operation), result, {})

    def test_actual_checkout_qualification_and_freshness(self):
        repository = StaticRepository()
        self.addCleanup(shutil.rmtree, repository.root)
        selected = dict(repository.request(), caller="alice", workflow=request()["workflow"])
        api = Provider()
        api.source = {"sha": selected["source"]["commit"], "tree": {"sha": selected["source"]["tree"]}}
        observed = admission.admit(selected, store(), CONTEXT, api)
        env = workspace.environment(repository.root)
        admission.qualify(repository.root, selected, observed, env)
        with self.assertRaises(Refusal):
            admission.qualify(repository.root, selected, dict(observed, intent="0" * 64), env)
        repository.write("node_modules/untrusted", "ignored")
        with self.assertRaises(Refusal):
            admission.qualify(repository.root, selected, observed, env)


if __name__ == "__main__":
    unittest.main()
