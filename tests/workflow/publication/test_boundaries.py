import http.client
import tempfile
import unittest
from itertools import product
from pathlib import Path
from unittest import mock

from lib.content.lane import codec
from lib.process.lane import execution, publication
from lib.refusal import Refusal
from lib.store.lane import public
from scripts import preview
from tests.workflow import fixtures as workflow
from tests.workflow.publication import fixtures
from tests.workflow.publication.test_publication import invoke


class Boundaries(unittest.TestCase):
    def test_all_foreign_writers_and_unneeded_reader_credentials_refuse_before_observation(self):
        names = ("CLOUDFLARE_API_TOKEN", "WHARF_LANE_STATE_ACCESS_KEY_ID", "WHARF_LANE_STATE_SECRET_ACCESS_KEY",
                 "WHARF_RELEASES_ACCESS_KEY_ID", "WHARF_RELEASES_SECRET_ACCESS_KEY", "WHARF_LANE_PROVIDER_TOKEN",
                 "WHARF_LANE_DEPLOYMENT_TOKEN", "WHARF_LANE_DISPOSAL_TOKEN", "WHARF_R2_ACCESS_KEY_ID", "WHARF_R2_SECRET_ACCESS_KEY",
                 "WHARF_CLOUDFLARE_TOKEN", "WHARF_LANE_PROVIDER_READER_TOKEN", "WHARF_LANE_WORKLOAD_READER_ACCESS_KEY_ID",
                 "WHARF_LANE_WORKLOAD_READER_SECRET_ACCESS_KEY")
        for name in names:
            with mock.patch.object(execution, "observe", side_effect=AssertionError("must refuse before observation")), self.subTest(name=name), self.assertRaises(Refusal):
                publication.perform({}, {name: "foreign-role"})
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "private"
            with path.open("w") as output:
                output.write("foreign-role")
            with mock.patch.object(execution, "observe", side_effect=AssertionError("file credentials must refuse first")), self.assertRaises(Refusal):
                publication.perform({}, {"WHARF_LANE_PROVIDER_TOKEN_FILE": str(path)})

    def test_registration_current_custody_permission_or_cancellation_movement_stops_reference(self):
        for failure, stage in product(("registration", "current", "custody", "permission", "cancelled"), (1, 3, 4, 5)):
            fixture = fixtures.bundle()
            fixture["control"] = workflow.provider()
            calls = []

            def observe(intent, value, environ):
                calls.append(intent)
                if len(calls) == stage:
                    if failure == "registration":
                        fixture["store"].target["enabled"] = False
                    elif failure == "current":
                        fixture["store"].current = None
                    elif failure == "custody":
                        fixture["store"].objects[execution.location(intent)] = b"{}"
                    elif failure == "permission":
                        fixture["control"].permission = lambda *args: {"permission": "read"}
                    else:
                        fixture["control"].observed.update(status="completed", conclusion="cancelled")
                return fixtures.observe(fixture, intent, value, environ)

            with mock.patch.object(execution, "observe", side_effect=observe), mock.patch.object(publication, "configured", return_value=fixture["bucket"]):
                with mock.patch.object(public.http.client, "HTTPSConnection", side_effect=lambda *args, **kwargs: fixtures.Connection(fixture)):
                    with self.subTest(failure=failure, stage=stage), self.assertRaises(Refusal):
                        publication.perform(fixture["held"], fixture["env"])
            self.assertEqual(len(fixture["bucket"].writes), 0 if stage <= 3 else 1)
            self.assertFalse(any(call[0] == "swap" for call in fixture["store"].calls))

    def test_writer_attempt_error_or_public_transport_error_cannot_be_masked(self):
        for failure in (OSError("private secret"), http.client.HTTPException("private secret"), Refusal("private secret")):
            fixture = fixtures.bundle()
            with mock.patch.object(fixture["bucket"], "create", side_effect=failure), self.assertRaises(Refusal) as caught:
                invoke(fixture)
            self.assertNotIn("private secret", str(caught.exception))
            self.assertNotIn("reads", fixture)
        for target in ("request", "getresponse", "close"):
            fixture = fixtures.bundle()
            with mock.patch.object(fixtures.Connection, target, side_effect=OSError("private transport")), self.subTest(target=target), self.assertRaises(Refusal):
                invoke(fixture)
            self.assertEqual(len(fixture["bucket"].writes), 1)

    def test_exact_writer_exposes_neither_general_keys_nor_mutable_store_operations(self):
        fixture = fixtures.bundle()
        record = {"schema": 2, "entry": {"kind": "operation", "value": fixture["operation"]}}
        reference = {"authority": "wharf", "digest": codec.digest("record", record)}
        selected = {"key": public.location(reference), "body": codec.encode("record", record)}
        with mock.patch.object(publication, "configured", return_value=fixture["bucket"]):
            writer = publication.Writer({}, selected, fixture["env"])
        for call in (lambda: writer.create("lane/v1/current.json", selected["body"], {}),
                     lambda: writer.create(selected["key"], b"changed", {}), lambda: writer.get("lane/v1/current.json")):
            with mock.patch.object(execution, "observe", side_effect=AssertionError("key/body must refuse before authority read")), self.assertRaises(Refusal):
                call()
        for name in ("put", "swap", "delete", "copy", "prefixes"):
            self.assertFalse(hasattr(writer, name))
        self.assertEqual(fixture["bucket"].writes, [])

    def test_prepared_evidence_configuration_has_no_generic_writer_fallback(self):
        env = {"WHARF_LANE_EVIDENCE_" + key: value for key, value in {
            "ENDPOINT": "https://" + "a" * 32 + ".r2.cloudflarestorage.com", "BUCKET": "lane-evidence",
            "ACCESS_KEY_ID": "evidence-id", "SECRET_ACCESS_KEY": "evidence-secret"}.items()}
        selected = publication.configured(env)
        self.assertEqual((selected.host, selected.name), ("a" * 32 + ".r2.cloudflarestorage.com", "lane-evidence"))
        for changed in ({"WHARF_LANE_EVIDENCE_ENDPOINT": "https://other.test"}, {"WHARF_LANE_EVIDENCE_BUCKET": "../state"},
                        {"WHARF_LANE_EVIDENCE_SECRET_ACCESS_KEY": ""}):
            with self.assertRaises(Refusal):
                publication.configured(dict(env, **changed))
        with self.assertRaises(Refusal):
            publication.configured({name.replace("WHARF_LANE_EVIDENCE_", "WHARF_R2_"): value for name, value in env.items()})

    def test_evidence_job_and_script_expose_only_reference_without_other_writers(self):
        root = Path(publication.__file__).resolve().parents[3]
        body = (root / ".github/workflows/evidence.yml").read_text()
        self.assertIn("workflow_call:", body)
        self.assertIn("value: ${{ jobs.evidence.outputs.reference }}", body)
        self.assertIn("ref: ${{ github.sha }}", body)
        job = body.split("\n  evidence:\n", 1)[1].split("\n  state:\n", 1)[0]
        for forbidden in ("workflow_dispatch", "push:", "schedule:", "secrets: inherit", "WHARF_R2_ACCESS_KEY_ID",
                          "WHARF_R2_SECRET_ACCESS_KEY", "WHARF_LANE_PROVIDER_TOKEN", "WHARF_LANE_DEPLOYMENT_TOKEN",
                          "WHARF_LANE_DISPOSAL_TOKEN", "WHARF_LANE_STATE_ACCESS_KEY_ID", "WHARF_LANE_WORKLOAD_", "preview-source", "upload-artifact"):
            self.assertNotIn(forbidden, job)
        for forbidden in ("workflow_dispatch", "push:", "schedule:", "secrets: inherit"):
            self.assertNotIn(forbidden, body)
        reference = {"authority": "wharf", "digest": "1" * 64}
        with mock.patch.object(publication, "perform", return_value=reference), mock.patch.object(preview.parameters, "answer", side_effect=lambda value: value):
            self.assertEqual(preview.lane_evidence({}), {"reference": codec.encode("reference", reference).decode()})
        self.assertTrue((root / ".github/workflows/lane.yml").exists())


if __name__ == "__main__":
    unittest.main()
