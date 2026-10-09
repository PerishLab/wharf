import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.content import canonical
from lib.process.follow import event, receiver
from lib.refusal import Refusal

SECRET = "fixture-webhook-secret"
PUSH = {"ref": "refs/heads/main", "deleted": False, "after": "a" * 40, "repository": {"id": 17, "full_name": "PerishLab/example", "default_branch": "main"}, "installation": {"id": 23}}
CONTEXT = {"GITHUB_REPOSITORY": "PerishLab/wharf", "GITHUB_WORKFLOW_REF": event.WORKFLOW, "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "repository_dispatch", "RUNNER_ENVIRONMENT": "github-hosted", "RUNNER_OS": "Linux", "WHARF_FOLLOW_SENDER_ID": "31", event.SECRET: SECRET}


def headers(body):
    return {"X-Hub-Signature-256": event.signature(body, SECRET), "X-GitHub-Event": "push", "X-GitHub-Delivery": "fixture-delivery"}


class Webhook(unittest.TestCase):
    def setUp(self):
        self.body = canonical.encode(PUSH)

    def test_authenticated_main_push_becomes_a_small_signed_dispatch(self):
        held = event.webhook(self.body, headers(self.body), SECRET)
        self.assertEqual(held["summary"]["repository_id"], 17)
        self.assertEqual(held["repository"], "PerishLab/example")
        self.assertLess(len(canonical.encode(held)), 1000)
        event.authenticate(canonical.encode(held["summary"]), held["signature"], SECRET, event.PURPOSE)

    def test_bad_or_modified_webhooks_send_nothing(self):
        for body, signed in ((self.body + b" ", headers(self.body)), (self.body, {})):
            with self.subTest(body=body), self.assertRaisesRegex(Refusal, "signature"):
                receiver.receive(body, signed, {event.SECRET: SECRET}, mock.Mock())

    def test_non_main_or_deleted_pushes_send_nothing(self):
        for change in ({"ref": "refs/heads/topic"}, {"deleted": True}):
            body = canonical.encode(dict(PUSH, **change))
            send = mock.Mock()
            self.assertEqual(receiver.receive(body, headers(body), {event.SECRET: SECRET}, send)["state"], "ignored")
            send.assert_not_called()

    def test_dispatch_has_its_own_purpose(self):
        payload = event.webhook(self.body, headers(self.body), SECRET)
        with self.assertRaisesRegex(Refusal, "signature"):
            event.authenticate(canonical.encode(payload["summary"]), payload["signature"], SECRET)

    def test_receiver_sends_only_the_authenticated_summary(self):
        send = mock.Mock()
        env = {event.SECRET: SECRET, receiver.TOKEN: "fixture-dispatch"}
        result = receiver.receive(self.body, headers(self.body), env, send)
        self.assertEqual(result["state"], "dispatched")
        self.assertEqual(set(send.call_args.args[0]), {"repository", "summary", "signature"})
        self.assertEqual(send.call_args.args[1], env)

    def test_repository_and_provider_identity_refuse_before_dispatch(self):
        invalid = ({"full_name": "Another/example"}, {"full_name": "PerishLab/.."}, {"id": True}, {"default_branch": "other"})
        for change in invalid:
            body = canonical.encode(dict(PUSH, repository=dict(PUSH["repository"], **change)))
            with self.subTest(change=change), self.assertRaises(Refusal):
                event.webhook(body, headers(body), SECRET)


class Admission(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "event.json"
        self.event = {"action": "follow-push", "sender": {"id": 31}, "client_payload": event.webhook(canonical.encode(PUSH), headers(canonical.encode(PUSH)), SECRET)}
        self.path.write_text(json.dumps(self.event))

    def test_exact_main_receiver_dispatch_is_admitted(self):
        self.assertEqual(event.qualified(self.path, CONTEXT)["repository"], "PerishLab/example")

    def test_other_actor_or_runtime_refuses(self):
        for field, value in (("GITHUB_REF", "refs/heads/topic"), ("GITHUB_WORKFLOW_REF", "another"), ("RUNNER_ENVIRONMENT", "self-hosted"), ("WHARF_FOLLOW_SENDER_ID", "32")):
            with self.subTest(field=field), self.assertRaises(Refusal):
                event.qualified(self.path, dict(CONTEXT, **{field: value}))

    def test_concurrency_target_tampering_refuses(self):
        self.event["client_payload"]["repository"] = "PerishLab/another"
        self.path.write_text(json.dumps(self.event))
        with self.assertRaisesRegex(Refusal, "concurrency target"):
            event.qualified(self.path, CONTEXT)

    def test_summary_tampering_refuses(self):
        self.event["client_payload"]["summary"]["after"] = "b" * 40
        self.path.write_text(json.dumps(self.event))
        with self.assertRaisesRegex(Refusal, "signature"):
            event.qualified(self.path, CONTEXT)
