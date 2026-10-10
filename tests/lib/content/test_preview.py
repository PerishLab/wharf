import copy
import unittest

from lib.content import canonical
from lib.content.lane import preview
from lib.refusal import Refusal


def target():
    return {
        "schema": "wharf.preview.registration/v1", "repository": "PerishLab/crest", "app": "crest-review",
        "account": "a" * 32, "worker": "crest-review", "parent": "registered-parent-id", "access": "public",
        "generation": "b" * 32, "enabled": True,
    }


def intent(operation="apply", revision=0, identity="c"):
    return {
        "schema": "wharf.preview.request/v1", "operation": operation, "repository": "PerishLab/crest",
        "app": "crest-review", "name": "crest-10", "revision": revision, "request": identity * 32,
        "registration": canonical.digest(target()), "caller": "provider-observed-caller", "workflow": "exact-workflow-identity",
        "source": {"commit": "d" * 40, "tree": "e" * 40, "declaration": "f" * 64} if operation == "apply" else None,
    }


def result(request, outcome="verified"):
    deployment = {
        "id": "deployment-id", "request": request["request"], "digest": "1" * 64,
        "latest_url": "https://crest-10-crest-review.example.workers.dev",
        "exact_url": "https://deployment-id-crest-review.example.workers.dev",
    }
    return {
        "schema": "wharf.preview.result/v1", "request": request["request"], "intent": canonical.digest(request),
        "outcome": outcome,
        "provider": {"state": "present", "deployment": deployment, "quiescent": True},
        "content": {"state": "verified", "digest": "1" * 64}, "failure": None,
    }


class Protocol(unittest.TestCase):
    def test_apply_inspect_discard(self):
        for operation in ("apply", "inspect", "discard"):
            with self.subTest(operation=operation):
                self.assertEqual(preview.request(intent(operation))["operation"], operation)

    def test_request_refusals(self):
        replacements = {
            "schema": "wharf.preview.request/v2", "operation": "deploy", "repository": "../crest",
            "app": "../app", "name": "a" * 49, "revision": True, "request": "bad", "registration": "bad",
            "caller": "", "workflow": "line\nbreak", "source": None,
        }
        for key, value in replacements.items():
            with self.subTest(key=key), self.assertRaises(Refusal):
                preview.request(dict(intent(), **{key: value}))

    def test_unknown_fields_and_source_shape(self):
        for value in (dict(intent(), command="shell"), dict(intent(), source={"commit": "d" * 40}), dict(intent("discard"), source=intent()["source"])):
            with self.subTest(value=value), self.assertRaises(Refusal):
                preview.request(value)

    def test_exact_target_and_generation(self):
        preview.admitted(intent(), target())
        for key, value in (("repository", "PerishLab/design"), ("account", "2" * 32), ("generation", "3" * 32), ("parent", "another-parent"), ("enabled", False), ("access", "private")):
            with self.subTest(key=key), self.assertRaises(Refusal):
                preview.admitted(intent(), dict(target(), **{key: value}))

    def test_registration_fields_are_explicit(self):
        for key in target():
            value = target()
            del value[key]
            with self.subTest(key=key), self.assertRaises(Refusal):
                preview.registration(value)

    def test_verification_needs_exact_request_and_bytes(self):
        request = intent()
        preview.result(result(request), request)
        changes = (
            ("request", "9" * 32), ("intent", "9" * 64), ("outcome", "ready"), ("failure", "failed"),
        )
        for key, value in changes:
            with self.subTest(key=key), self.assertRaises(Refusal):
                preview.result(dict(result(request), **{key: value}), request)
        value = result(request)
        value["content"]["digest"] = "2" * 64
        with self.assertRaises(Refusal):
            preview.result(value, request)
        value = result(request)
        value["provider"]["deployment"]["request"] = "2" * 32
        with self.assertRaises(Refusal):
            preview.result(value, request)

    def test_native_urls_only(self):
        deployment = result(intent())["provider"]["deployment"]
        urls = ("http://example.workers.dev", "https://example.com", "https://user@example.workers.dev", "https://example.workers.dev:443", "https://example.workers.dev/x", "https://example.workers.dev?q=1", "https://[")
        for url in urls:
            with self.subTest(url=url), self.assertRaises(Refusal):
                preview.deployment(dict(deployment, latest_url=url))

    def test_unknown_and_uncertain_are_not_terminal(self):
        value = result(intent())
        value["provider"]["quiescent"] = False
        with self.assertRaises(Refusal):
            preview.result(value, intent())
        value.update(outcome="unknown", failure="upload response lost")
        value["content"]["state"] = "unknown"
        preview.result(value, intent())

    def test_discard_needs_provider_and_url_absence(self):
        request = intent("discard")
        value = result(request, "discarded")
        value["provider"].update(state="absent", deployment=None)
        value["content"].update(state="gone", digest=None)
        preview.result(value, request)
        for state in ("unverified", "unknown", "verified"):
            broken = copy.deepcopy(value)
            broken["content"]["state"] = state
            with self.subTest(state=state), self.assertRaises(Refusal):
                preview.result(broken, request)

    def test_incomplete_result_explains_failure(self):
        for outcome in ("degraded", "failed", "unknown"):
            value = result(intent(), outcome)
            value["content"]["state"] = "unverified"
            with self.subTest(outcome=outcome), self.assertRaises(Refusal):
                preview.result(value, intent())


if __name__ == "__main__":
    unittest.main()
