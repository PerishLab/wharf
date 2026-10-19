import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.content import canonical
from lib.process.cfworker import discard, writer
from lib.process.lane import disposal, entry, execution, reservation
from lib.refusal import Refusal
from tests.lib.preview.test_admission import CONTEXT
from tests.lib.process.test_cfworker import Response
from tests.workflow import fixtures
from tests.workflow.provider import test_provider


class Disposal(unittest.TestCase):
    def prepared(self):
        source = test_provider.Provider()
        self.addCleanup(source.doCleanups)
        fixture, _, _, _, env, _ = source.present()
        registered = fixture.registered
        intent = fixtures.request("dispose", registered)
        store = fixtures.Store(registered, fixtures.settled(registered))
        observed = entry.admit(intent, store, CONTEXT, fixtures.provider())
        with mock.patch.object(entry, "observe", return_value=observed), mock.patch.object(reservation.r2, "configured", return_value=store):
            value = reservation.prepare({"request": fixtures.encoded(intent), "artifact": "null"}, env)
        directory = self.enterContext(tempfile.TemporaryDirectory())
        env.update(GITHUB_JOB="dispose", RUNNER_TEMP=directory, WHARF_LANE_PROVIDER_TOKEN="dispose-writer")
        before = fixture.observed
        plan = discard.prepare(registered, value["current"], before, fixture.plan["policy"])
        observed = execution.admit(intent, value, store, (CONTEXT, fixtures.provider()))
        projected = {"execution": canonical.digest(observed), "plan": plan, "operation": None}
        held = {"request": fixtures.encoded(intent), "prepared": fixtures.encoded(value), "provider": fixtures.encoded(projected)}
        api = mock.Mock()
        api.status.side_effect = [before, before, dict(before, preview=None, previews=[before["previews"][1]])]
        return store, env, held, api

    @staticmethod
    def invoke(store, env, held, trusted):
        api, transport = trusted
        actual = writer.Writer

        def observe(intent, value, environ):
            return execution.admit(intent, value, store, (entry.platform(environ), fixtures.provider()))

        def owned(token, plan):
            result = actual(token, plan)
            result._client = transport
            return result

        with mock.patch.object(execution, "observe", side_effect=observe), mock.patch.object(disposal.reader, "Reader", return_value=api):
            with mock.patch.object(writer, "Writer", side_effect=owned):
                return disposal.perform(held, env)

    @staticmethod
    def transport():
        api = mock.Mock()
        api.open.side_effect = lambda request, timeout: Response(b'{"success":true,"errors":[],"result":null}', request.full_url)
        return api

    def test_one_exact_delete_and_independent_absence_do_not_settle_private_state(self):
        store, env, held, api = self.prepared()
        transport = self.transport()
        previous = copy.deepcopy(store.current)
        result = self.invoke(store, env, held, (api, transport))
        self.assertEqual(result["operation"]["outcome"], {"outcome": "applied", "material": {"state": "absent"}, "writer": "idle"})
        self.assertEqual(transport.open.call_count, 1)
        request = transport.open.call_args.args[0]
        self.assertEqual(request.get_method(), "DELETE")
        self.assertTrue(request.full_url.endswith("/previews/selected-id"))
        self.assertEqual(api.status.call_count, 3)
        self.assertTrue(all("domain" in call.args[0] for call in api.status.call_args_list))
        self.assertEqual(store.current, previous)
        self.assertEqual(len(list(Path(env["RUNNER_TEMP"]).iterdir())), 1)

    def test_response_loss_preserves_unknown_and_local_attempt_cannot_repeat(self):
        store, env, held, api = self.prepared()
        transport = self.transport()
        transport.open.side_effect = TimeoutError("lost")
        before = disposal.evidence.decode(held["provider"])["plan"]["before"]
        api.status.side_effect = None
        api.status.return_value = before
        result = self.invoke(store, env, held, (api, transport))
        self.assertEqual(result["operation"]["outcome"], {"outcome": "unknown", "reason": "unverified"})
        with self.assertRaises(Refusal):
            self.invoke(store, env, held, (api, transport))
        self.assertEqual(transport.open.call_count, 1)
        self.assertEqual(store.current["phase"], "reserved")

    def test_acknowledgement_without_absence_or_unchanged_siblings_is_not_applied(self):
        for failure in ("present", "siblings"):
            store, env, held, api = self.prepared()
            plan = disposal.evidence.decode(held["provider"])["plan"]
            before = plan["before"]
            after = before if failure == "present" else dict(before, preview=None, previews=[])
            api.status.side_effect = [before, before, after]
            transport = self.transport()
            with self.subTest(failure=failure), self.assertRaises(Refusal):
                self.invoke(store, env, held, (api, transport))
            self.assertEqual(transport.open.call_count, 1)
            self.assertEqual(store.current["phase"], "reserved")

    def test_foreign_attempt_job_or_preflight_never_deletes(self):
        for failure in ("attempt", "job", "preflight"):
            store, env, held, api = self.prepared()
            if failure == "attempt":
                env["GITHUB_RUN_ATTEMPT"] = "2"
            elif failure == "job":
                env["GITHUB_JOB"] = "build"
            else:
                projected = disposal.evidence.decode(held["provider"])
                projected["execution"] = "0" * 64
                held["provider"] = fixtures.encoded(projected)
            transport = self.transport()
            with self.subTest(failure=failure), self.assertRaises(Refusal):
                self.invoke(store, env, held, (api, transport))
            transport.open.assert_not_called()

    def test_local_marker_failure_and_lost_authority_block_new_attempt(self):
        store, env, held, api = self.prepared()
        transport = self.transport()
        with mock.patch.object(disposal.os, "fsync", side_effect=OSError("lost")), self.assertRaises(Refusal):
            self.invoke(store, env, held, (api, transport))
        transport.open.assert_not_called()
        self.assertEqual(len(list(Path(env["RUNNER_TEMP"]).iterdir())), 1)
        store, env, held, api = self.prepared()
        before = disposal.evidence.decode(held["provider"])["plan"]["before"]

        def moved(policy):
            store.target["enabled"] = False
            return before

        api.status.side_effect = moved
        with self.assertRaises(Refusal):
            self.invoke(store, env, held, (api, transport))
        transport.open.assert_not_called()

    def test_reusable_disposal_has_no_storage_writer_or_partial_trigger(self):
        with self.assertRaises(Refusal):
            disposal.perform({}, {"WHARF_R2_SECRET_ACCESS_KEY": "writer"})
        root = Path(disposal.__file__).resolve().parents[3]
        workflow = (root / ".github/workflows/dispose.yml").read_text()
        self.assertIn("workflow_call:", workflow)
        self.assertIn("value: ${{ jobs.dispose.outputs.result }}", workflow)
        self.assertIn("WHARF_LANE_DISPOSAL_TOKEN:", workflow)
        self.assertNotIn("workflow_dispatch", workflow)
        self.assertNotIn("secrets: inherit", workflow)
        self.assertNotIn("WHARF_LANE_STATE_SECRET", workflow)
        self.assertNotIn("WHARF_LANE_EVIDENCE_SECRET", workflow)
        self.assertNotIn("preview-source", workflow)

    def test_authority_loss_at_write_or_readback_preserves_reservation(self):
        for stage in (2, 3):
            store, env, held, api = self.prepared()
            before = disposal.evidence.decode(held["provider"])["plan"]["before"]
            calls = []

            def moved(policy):
                calls.append(policy)
                if len(calls) == stage:
                    store.target["enabled"] = False
                return before if len(calls) < 3 else dict(before, preview=None, previews=[before["previews"][1]])

            api.status.side_effect = moved
            transport = self.transport()
            with self.subTest(stage=stage), self.assertRaises(Refusal):
                self.invoke(store, env, held, (api, transport))
            self.assertEqual(transport.open.call_count, 0 if stage == 2 else 1)
            self.assertEqual(store.current["phase"], "reserved")
            self.assertEqual(len(list(Path(env["RUNNER_TEMP"]).iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
