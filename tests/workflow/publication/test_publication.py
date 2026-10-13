import copy
import json
import unittest
from unittest import mock

from lib.content.lane import codec
from lib.process.lane import execution, publication
from lib.refusal import Refusal
from lib.store.lane import public, publication as kernel
from tests.workflow import fixtures as workflow
from tests.workflow.publication import fixtures


def invoke(fixture):
    with mock.patch.object(execution, "observe", side_effect=lambda *args: fixtures.observe(fixture, *args)):
        with mock.patch.object(publication, "configured", return_value=fixture["bucket"]):
            with mock.patch.object(public.http.client, "HTTPSConnection", side_effect=lambda *args, **kwargs: fixtures.Connection(fixture)):
                return publication.perform(fixture["held"], fixture["env"])


class Publication(unittest.TestCase):
    def test_all_actions_publish_only_exact_shared_record_without_state_mutation(self):
        for action in ("apply", "inspect", "dispose"):
            fixture = fixtures.bundle(action)
            before = copy.deepcopy(fixture["store"].current)
            result = invoke(fixture)
            key = public.location(result)
            body = fixture["bucket"].get(key)
            record = codec.verify(result, result, body)
            self.assertEqual(record, {"schema": 2, "entry": {"kind": "operation", "value": fixture["operation"]}})
            self.assertEqual(fixture["store"].current, before)
            self.assertEqual(fixture["bucket"].writes, [key])
            self.assertEqual(len(fixture["reads"]), 2)
            for method, path, headers in fixture["reads"]:
                self.assertEqual((method, path), ("GET", "/" + key))
                self.assertEqual(headers, {"Accept": "application/json", "Accept-Encoding": "identity"})
            self.assertTrue(fixture["closed"])
            self.assertNotIn(b"provider-plan-do-not-publish", body)
            self.assertNotIn("plan", result)
            self.assertFalse(any(call[0] in ("swap", "create") for call in fixture["store"].calls))

    def test_non_applied_evidence_remains_non_completion(self):
        for outcome in ("unknown", "refused"):
            fixture = fixtures.bundle(outcome=outcome)
            result = invoke(fixture)
            body = fixture["bucket"].get(public.location(result))
            record = codec.verify(result, result, body)
            self.assertEqual(record["entry"]["value"]["outcome"], fixture["operation"]["outcome"])
            self.assertEqual(fixture["store"].current["phase"], "reserved")
            with mock.patch.object(public.Reader, "read", return_value=body), self.assertRaises(Refusal):
                kernel.settle(public.prepare({"authority": "wharf", "origin": fixture["env"]["WHARF_LANE_PUBLICATION_ORIGIN"]}),
                              fixture["value"]["registration"]["declaration"], result, fixture["operation"]["request"])

    def test_result_hash_request_assessment_and_private_operation_fields_refuse_before_write(self):
        for failure in ("execution", "request", "assessment", "private", "absent"):
            fixture = fixtures.bundle()
            supplied = json.loads(fixture["held"]["provider"])
            if failure == "execution":
                supplied["execution"] = "0" * 64
            elif failure == "request":
                supplied["operation"]["request"]["context"]["request"] = "0" * 64
                supplied["operation"]["assessment"]["request"] = codec.digest("request", supplied["operation"]["request"])
            elif failure == "assessment":
                supplied["operation"]["assessment"]["conditions"]["authorization"]["evidence"]["digest"] = "0" * 64
            elif failure == "private":
                supplied["operation"]["outcome"]["token"] = "completion-jwt"
            else:
                supplied["operation"] = None
            fixture["held"]["provider"] = workflow.encoded(supplied)
            with self.subTest(failure=failure), self.assertRaises(Refusal):
                invoke(fixture)
            self.assertEqual(fixture["bucket"].writes, [])
            self.assertNotIn("reads", fixture)

    def test_malformed_duplicate_oversize_or_foreign_envelope_refuses(self):
        for body in ("null", "{", '{"execution":1,"execution":2,"plan":{},"operation":{}}', " " * 1048577):
            fixture = fixtures.bundle()
            fixture["held"]["provider"] = body
            with self.assertRaises(Refusal):
                invoke(fixture)
            self.assertEqual(fixture["bucket"].writes, [])
        fixture = fixtures.bundle()
        supplied = json.loads(fixture["held"]["provider"])
        supplied["credentials"] = "private"
        fixture["held"]["provider"] = workflow.encoded(supplied)
        with self.assertRaises(Refusal):
            invoke(fixture)
        self.assertEqual(fixture["bucket"].writes, [])

    def test_authority_origin_job_or_foreign_attempt_never_authorizes_publication(self):
        changes = ({"WHARF_LANE_PUBLICATION_AUTHORITY": "other"}, {"WHARF_LANE_PUBLICATION_ORIGIN": "https://evidence.example.test/path"},
                   {"GITHUB_JOB": "apply"}, {"GITHUB_RUN_ATTEMPT": "2"}, {"GITHUB_RUN_ID": "124"})
        for changed in changes:
            fixture = fixtures.bundle()
            fixture["env"].update(changed)
            with self.subTest(changed=changed), self.assertRaises(Refusal):
                invoke(fixture)
            self.assertEqual(fixture["bucket"].writes, [])

    def test_existing_identical_bytes_need_fresh_independent_reads(self):
        fixture = fixtures.bundle()
        first = invoke(fixture)
        self.assertEqual(invoke(fixture), first)
        self.assertEqual(len(fixture["bucket"].writes), 1)
        self.assertEqual(len(fixture["reads"]), 4)
        fixture["response"] = fixtures.Response(b"changed")
        with self.assertRaises(Refusal):
            invoke(fixture)
        self.assertEqual(len(fixture["bucket"].writes), 1)

    def test_different_private_collision_refuses_even_with_matching_public_content(self):
        fixture = fixtures.bundle()
        reference = invoke(fixture)
        body = fixture["bucket"].get(public.location(reference))
        fixture["bucket"].objects[public.location(reference)] = b"different"
        fixture["response"] = fixtures.Response(body)
        count = len(fixture["reads"])
        with self.assertRaises(Refusal):
            invoke(fixture)
        self.assertEqual(len(fixture["reads"]), count)

    def test_public_failure_never_falls_back_to_successful_private_write(self):
        for response in (fixtures.Response(b"altered"), fixtures.Response(b"", 302), fixtures.Response(b"", 404),
                         fixtures.Response(b"", 500), fixtures.Response(b"", encoding="gzip"), fixtures.Response(b" " * 65537)):
            fixture = fixtures.bundle()
            fixture["response"] = response
            with self.subTest(status=response.status), self.assertRaises(Refusal):
                invoke(fixture)
            self.assertEqual(len(fixture["bucket"].writes), 1)
            self.assertEqual(fixture["store"].current["phase"], "reserved")

    def test_second_public_read_movement_never_returns_reference(self):
        fixture = fixtures.bundle()
        original = fixtures.Connection.getresponse

        def moved(connection):
            response = original(connection)
            if len(fixture.get("reads", [])) == 2:
                return fixtures.Response(b"changed")
            return response

        with mock.patch.object(fixtures.Connection, "getresponse", side_effect=moved, autospec=True), self.assertRaises(Refusal):
            invoke(fixture)
        self.assertEqual(len(fixture["bucket"].writes), 1)


if __name__ == "__main__":
    unittest.main()
