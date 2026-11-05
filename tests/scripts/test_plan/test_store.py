from contextlib import ExitStack, contextmanager
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from lib.refusal import Refusal
from scripts import plan
from tests.lib.store.memory import Memory


class Credentials:
    access_key = "0123456789abcdef" * 2

    @property
    def secret_key(self):
        raise AssertionError("observation accessed the secret")


class Store(unittest.TestCase):
    def test_planner_output_binds_the_exact_stored_plan(self):
        bucket = self.bucket()
        result, outputs = self.observe(bucket)
        reference = next(json.loads(call.args[0]["plan-reference"]) for call in outputs if "plan-reference" in call.args[0])
        document = json.loads(bucket.get(result["plan"]))
        self.assertEqual(reference, plan.plan.reference(document))

    def bucket(self):
        bucket = Memory()
        bucket.host = "a" * 32 + ".r2.cloudflarestorage.com"
        bucket.name = "perish-wharf-workload"
        bucket.credentials = Credentials()
        return bucket

    def observe(self, bucket):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            (root / "plumb.toml").write_text("[release]\n")
            held = dict(repository="PerishLab/demo", marker="v1.2.3", commit="a" * 40, tree="b" * 40, wharf="c" * 40, run="7", attempt="1", source=directory, steps=json.dumps({name: {"outputs": {"decision": "skip", "presence": "none"}} for name in plan.MEDIA}))
            identity = {name: held[name] for name in plan.IDENTITY}

            @contextmanager
            def prepared(request):
                yield root, dict(schema="wharf.release.snapshot/v1", source=identity, head="d" * 40, tree=held["tree"], packages=[], controls={}, domain={}, guard={}), lambda: None

            patches = [(plan.snapshot, "prepared", prepared), (plan.toolchain, "versions", lambda: {}), (plan.r2, "configured", lambda: bucket), (plan.basis, "carried", lambda source: False), (plan.trial, "capture", lambda source, identity: None), (plan.node, "carried", lambda source: False), (plan.cfworker, "workers", lambda source: []), (plan.preflight, "planning", lambda *args: None)]
            for target, name, value in patches:
                stack.enter_context(mock.patch.object(target, name, value))
            answer = stack.enter_context(mock.patch.object(plan.parameters, "answer"))
            result = plan.record(held)
            return result, answer.call_args_list

    def test_success_observes_only_after_all_existing_storage_and_keeps_bytes_and_outputs(self):
        bucket = self.bucket()
        baseline = Memory()
        without, old_outputs = self.observe(baseline)
        original = plan.storage

        def observed(held):
            self.assertTrue(any(name.startswith("verified/") for name in held.objects))
            self.assertEqual(held.objects, baseline.objects)
            return original(held)

        with mock.patch.object(plan, "storage", side_effect=observed) as observer:
            result, outputs = self.observe(bucket)
        observer.assert_called_once_with(bucket)
        self.assertNotIn(Credentials.access_key, json.dumps(result))
        self.assertEqual(result.pop("storage"), dict(schema="wharf.store.reference/v1", state="observed", host=bucket.host, bucket=bucket.name, access_sha256=hashlib.sha256(Credentials.access_key.encode()).hexdigest()))
        without.pop("storage")
        self.assertEqual(result, without)
        self.assertEqual(outputs, old_outputs)
        self.assertEqual(bucket.objects, baseline.objects)
        self.assertEqual(bucket.writes, baseline.writes)
        self.assertNotIn(Credentials.access_key, json.dumps(bucket.objects, default=str) + json.dumps(outputs, default=str))

    def test_unsupported_metadata_is_unavailable_without_echo_or_secret_access(self):
        for field, value in (("host", "secret.invalid"), ("name", "secret/invalid"), ("credentials", SimpleNamespace(access_key="secret-invalid")), ("host", None), ("name", 123), ("credentials", None)):
            with self.subTest(field=field, value=value):
                bucket = self.bucket()
                setattr(bucket, field, value)
                result, outputs = self.observe(bucket)
                self.assertEqual(result["storage"], dict(schema="wharf.store.reference/v1", state="unavailable"))
                self.assertNotIn("secret", json.dumps(result))
                self.assertTrue(bucket.objects)

    def test_failed_plan_or_remember_storage_never_observes_identity(self):
        for stage in ("plan", "standing", "verified"):
            with self.subTest(stage=stage):
                bucket = self.bucket()
                put = bucket.put

                def fail(key, body, headers=None):
                    if (stage == "plan" and key.endswith("/7-1.json")) or (stage == "standing" and key.endswith("/latest.json")) or (stage == "verified" and key.startswith("verified/")):
                        raise Refusal("storage unavailable")
                    put(key, body, headers)

                with mock.patch.object(bucket, "put", side_effect=fail), mock.patch.object(plan, "storage", side_effect=AssertionError("failed storage produced an observation")) as observer:
                    with self.assertRaisesRegex(Refusal, "storage unavailable"):
                        self.observe(bucket)
                observer.assert_not_called()
