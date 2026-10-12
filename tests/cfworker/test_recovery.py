import copy
import unittest
from unittest import mock

from lib.content.lane import codec
from lib.process.cfworker import apply, create, payload, reader, upload, writer
from lib.refusal import Refusal
from lib.store.lane import state
from tests.cfworker import test_deployment, test_readback
from tests.lib.process import test_cfworker_upload


class Recovery(unittest.TestCase):
    def setUp(self):
        self.fixture = test_deployment.Deployment()
        self.fixture.setUp()
        self.plan = self.fixture.plan
        self.deployed = self.fixture.projected()
        self.after = dict(self.fixture.before, deployments=[self.deployed])

    def resumed(self, plan=None, reason="unavailable"):
        plan = self.plan if plan is None else plan
        result = apply.unknown(plan, reason)
        current = state.finished(plan["apply"]["current"], result, plan["apply"]["registration"])
        return apply.resume(plan, current)

    def test_exact_unknown_request_resumes_only_observation(self):
        for reason in ("unavailable", "unverified"):
            plan = self.resumed(reason=reason)
            current = plan["apply"]["current"]
            self.assertEqual(current["phase"], "unknown")
            for key in ("revision", "active", "observed", "registration", "target"):
                self.assertEqual(current[key], self.plan["apply"]["current"][key])
            self.assertEqual(plan["before"], self.plan["before"])
            self.assertEqual(plan["policy"], self.plan["policy"])
            self.assertEqual(apply.resume(plan, current), plan)
            self.assertEqual(apply.candidate(plan, self.after), self.deployed)

    def test_resumed_reader_verifies_actual_exact_bytes_without_writer_custody(self):
        fixture = test_readback.Readback()
        fixture.setUp()
        fixture.plan = self.resumed()
        api = fixture.client()
        result = fixture.invoke(api)
        self.assertEqual(result["deployment"], self.deployed)
        self.assertEqual(result["request"], codec.digest("request", self.plan["apply"]["current"]["active"]["request"]))
        self.assertFalse(hasattr(api, "_token"))
        self.assertEqual(apply.readback(fixture.plan, fixture.after, result), result)

    def test_all_write_preparations_still_refuse_unknown_reservations(self):
        plan = self.resumed()
        material = plan["apply"]
        with mock.patch("urllib.request.build_opener") as opener:
            calls = (lambda: apply.checked(material), lambda: apply.deployment_checked(plan),
                     lambda: payload.manifest(material, self.fixture.document, self.fixture.bodies.__getitem__),
                     lambda: payload.deployment(material, self.fixture.document, self.fixture.bodies.__getitem__, test_cfworker_upload.FINAL),
                     lambda: upload.Uploader("private", material), lambda: writer.Deployer("private", plan, test_cfworker_upload.FINAL),
                     lambda: create.prepare(material, {key: plan["before"][key] for key in ("parent", "previews", "preview")}, plan["policy"]))
            for call in calls:
                with self.assertRaises(Refusal):
                    call()
            opener.assert_not_called()

    def test_original_unknown_revision_cannot_be_replaced_by_competitor(self):
        foreign = copy.deepcopy(self.plan)
        active = foreign["apply"]["current"]["active"]
        active["request"]["context"]["request"] = "7" * 64
        active["assessment"]["request"] = codec.digest("request", active["request"])
        current = state.finished(foreign["apply"]["current"], apply.unknown(foreign, "unavailable"), foreign["apply"]["registration"])
        with self.assertRaisesRegex(Refusal, "original reservation"):
            apply.resume(self.plan, current)

    def test_reserved_settled_refused_and_erased_history_are_not_unknown_recovery(self):
        current = self.plan["apply"]["current"]
        registered = self.plan["apply"]["registration"]
        refused = codec.read("operation", dict(current["active"], outcome={"outcome": "refused", "reason": "conflict"}))
        for held in (current, state.initial(registered), state.finished(current, refused, registered)):
            with self.assertRaises(Refusal):
                apply.resume(self.plan, held)
        held = self.resumed()["apply"]["current"]
        held["last"] = None
        with self.assertRaises(Refusal):
            apply.resume(self.plan, held)

    def test_unknown_plan_retains_previous_material_without_claiming_current_url(self):
        plan = self.fixture.populated()
        current = self.resumed(plan, "unverified")["apply"]["current"]
        self.assertEqual(current["observed"], plan["apply"]["current"]["observed"])
        self.assertEqual(current["revision"], plan["apply"]["current"]["revision"])
        self.assertEqual(current["last"]["outcome"], {"outcome": "unknown", "reason": "unverified"})
        with self.assertRaises(Refusal):
            apply.checked(dict(plan["apply"], current=current))

    def test_ambiguous_empty_foreign_or_evicted_catalog_never_chooses_latest(self):
        plan = self.fixture.populated()
        resumed = self.resumed(plan)
        after = dict(plan["before"], deployments=plan["before"]["deployments"] + [self.deployed])
        self.assertEqual(apply.candidate(resumed, after), self.deployed)
        for observed in (plan["before"], dict(after, deployments=[self.deployed]),
                         dict(after, deployments=after["deployments"] + [self.fixture.projected(2)]),
                         dict(after, previews=[plan["before"]["preview"]]), dict(after, preview=None)):
            with self.assertRaises(Refusal):
                apply.candidate(resumed, observed)

    def test_candidate_and_resume_are_detached_finite_values(self):
        plan = self.resumed()
        result = apply.candidate(plan, self.after)
        result["id"] = "changed"
        self.assertEqual(apply.candidate(plan, self.after), self.deployed)
        plan["before"]["parent"]["id"] = "changed"
        self.assertNotEqual(self.plan["before"]["parent"]["id"], "changed")
        with self.assertRaises(Refusal):
            apply.resume(dict(self.plan, deadline=0), self.resumed()["apply"]["current"])

    def test_registration_revision_material_and_assessment_movement_refuse(self):
        current = self.resumed()["apply"]["current"]
        for field, value in (("registration", "0" * 64), ("revision", 99), ("observed", {"state": "present", "revision": 1, "digest": "0" * 64})):
            with self.assertRaises(Refusal):
                apply.resume(self.plan, dict(current, **{field: value}))
        changed = copy.deepcopy(self.plan)
        changed["apply"]["registration"]["generation"] = "8" * 64
        with self.assertRaises(Refusal):
            apply.resume(changed, current)
        changed = copy.deepcopy(current)
        changed["active"]["assessment"]["conditions"]["authorization"]["evidence"]["digest"] = "0" * 64
        with self.assertRaises(Refusal):
            apply.resume(self.plan, changed)
