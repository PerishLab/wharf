import copy
import hashlib
import json
import unittest
from email import policy
from email.parser import BytesParser
from unittest import mock

from lib.content import canonical
from lib.content.lane import codec
from lib.content.static import assets
from lib.process.cfworker import apply, payload
from lib.refusal import Refusal
from lib.store import handoff
from lib.store.lane import state
from tests.lib.content.test_assets import FILES
from tests.lib.store.test_handoff import receipt
from tests.lib.store.test_lane import operation, registered


class Apply(unittest.TestCase):
    def setUp(self):
        self.receipt = receipt()
        self.receipt["app"] = "crest"
        self.document, self.files = handoff.describe(FILES, self.receipt)
        self.bodies = {hashlib.sha256(body).hexdigest(): body for body in self.files.values()}
        self.plan = self.prepared()

    def prepared(self, lane="preview"):
        value = operation(lane)
        value["request"]["intent"]["artifact"] = apply.identity(self.document)
        value["assessment"]["request"] = codec.digest("request", value["request"])
        held = registered(value)
        held["generation"] = self.receipt["source"]["declaration"]
        active = {key: value[key] for key in ("request", "assessment")}
        reserved = state.started(state.initial(held), active, held)
        return apply.prepare(held, reserved, self.document, self.bodies.__getitem__)

    def multipart(self, plan=None, completion="prepared.private.completion"):
        headers, body = payload.deployment(plan or self.plan, self.document, self.bodies.__getitem__, completion)
        message = BytesParser(policy=policy.default).parsebytes(("Content-Type: " + headers["Content-Type"] + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + body)
        return headers, body, list(message.iter_parts())

    def test_three_explicit_plans_bind_exact_material_and_generation(self):
        for lane in ("preview", "design", "review"):
            plan = self.prepared(lane)
            self.assertEqual(plan["current"]["active"]["request"]["context"]["requested"]["lane"], lane)
            self.assertEqual(plan["artifact"]["digest"], self.document["content"])
            self.assertEqual(plan["artifact"]["source"], {key: self.receipt["source"][key] for key in ("repository", "commit", "tree")})
            self.assertEqual(plan["artifact"]["build"]["inputs"], self.document["key"])
            expected = canonical.digest({key: self.receipt[key] for key in ("tools", "implementation")})
            self.assertEqual(plan["artifact"]["build"]["world"], expected)
            self.assertEqual(plan["registration"]["generation"], self.receipt["source"]["declaration"])
            self.assertEqual(apply.qualified(plan, self.document, self.bodies.__getitem__)[1], self.files)

    def test_hash_vectors_match_official_node_sha256_example(self):
        for name, expected in (("index.html", "5deeda9d2277e0ed541bfc03adafb3cf"), ("style.css", "76e0cb1d9afc75c891dc20261a6eff05"), ("identity", "333d6b3a3c1f5db6c9bdda5939b13698")):
            self.assertEqual(payload.digest(name, b"hello"), expected)

    def test_manifest_includes_identity_not_headers_and_is_detached(self):
        result = payload.manifest(self.plan, self.document, self.bodies.__getitem__)
        self.assertEqual(set(result), {"/" + name for name in self.files if name != "_headers"})
        self.assertIn("/" + assets.IDENTITY, result)
        for name, entry in result.items():
            body = self.files[name[1:]]
            self.assertEqual(entry, {"hash": payload.digest(name[1:], body), "size": len(body)})
            self.assertEqual(len(entry["hash"]), 32)
        result["/index.html"]["hash"] = "0" * 32
        self.assertNotEqual(payload.manifest(self.plan, self.document, self.bodies.__getitem__)["/index.html"]["hash"], "0" * 32)
        self.assertEqual(len(self.plan["artifact"]["digest"]), 64)

    def test_multipart_is_assets_only_and_preserves_exact_fixed_headers(self):
        headers, body, parts = self.multipart()
        self.assertEqual(headers["Content-Length"], str(len(body)))
        self.assertEqual(len(parts), 2)
        metadata = json.loads(parts[0].get_payload(decode=True))
        self.assertEqual(parts[0].get_param("name", header="content-disposition"), "metadata")
        self.assertEqual(metadata["assets"], {"jwt": "prepared.private.completion", "config": payload.CONFIG})
        self.assertEqual(set(metadata), {"assets", "annotations"})
        self.assertEqual(metadata["annotations"]["workers/message"], codec.digest("request", self.plan["current"]["active"]["request"]))
        self.assertEqual(metadata["annotations"]["workers/tag"], self.document["content"])
        self.assertEqual(parts[1].get_param("name", header="content-disposition"), "files")
        self.assertEqual(parts[1].get_filename(), "_headers")
        self.assertEqual(parts[1].get_content_type(), "text/plain")
        self.assertEqual(parts[1].get_payload(decode=True), assets.HEADERS)
        self.assertNotIn(b"main_module", body)
        self.assertNotIn(b"migrations", body)
        self.assertNotIn(b"bindings", body)

    def test_private_completion_never_enters_plan_or_manifest(self):
        self.multipart()
        self.assertNotIn("prepared.private.completion", json.dumps(self.plan))
        self.assertNotIn("prepared.private.completion", json.dumps(payload.manifest(self.plan, self.document, self.bodies.__getitem__)))
        for completion in (None, True, "", "with space", "a.b", "a.b.c\r\nheader", "非ascii", "a.b." + "c" * 8192):
            with self.subTest(completion=type(completion).__name__), self.assertRaises(Refusal):
                self.multipart(completion=completion)

    def test_each_serialization_revalidates_exact_content_after_prepare(self):
        name = self.document["files"]["index.html"]["blob"]
        self.bodies[name] = b"changed"
        for invoke in (lambda: apply.qualified(self.plan, self.document, self.bodies.__getitem__), lambda: payload.manifest(self.plan, self.document, self.bodies.__getitem__), self.multipart):
            with self.assertRaises(Refusal):
                invoke()

    def test_changed_app_repository_or_generation_refuses(self):
        for field, value in (("app", "other"), ("repository", "PerishLab/other")):
            changed = copy.deepcopy(self.receipt)
            if field == "app":
                changed[field] = value
            else:
                changed["source"][field] = value
                changed["guard"]["guard"]["repository"] = value
            document, files = handoff.describe(FILES, changed)
            bodies = {hashlib.sha256(body).hexdigest(): body for body in files.values()}
            with self.subTest(field=field), self.assertRaises(Refusal):
                apply.prepare(self.plan["registration"], self.plan["current"], document, bodies.__getitem__)
        plan = copy.deepcopy(self.plan)
        plan["registration"]["generation"] = "0" * 64
        with self.assertRaises(Refusal):
            apply.qualified(plan, self.document, self.bodies.__getitem__)

    def test_stale_disabled_unreserved_unknown_and_non_deploy_never_prepare_payload(self):
        for key, value in (("phase", "vacant"), ("revision", 2), ("active", None), ("registration", "0" * 64)):
            plan = copy.deepcopy(self.plan)
            plan["current"][key] = value
            with self.subTest(key=key), self.assertRaises(Refusal):
                apply.checked(plan)
        plan = copy.deepcopy(self.plan)
        plan["registration"]["enabled"] = False
        with self.assertRaises(Refusal):
            apply.checked(plan)
        plan = copy.deepcopy(self.plan)
        unknown = dict(plan["current"]["active"], outcome={"outcome": "unknown", "reason": "unavailable"})
        plan["current"] = state.finished(plan["current"], unknown, plan["registration"])
        with self.assertRaises(Refusal):
            self.multipart(plan)
        plan = copy.deepcopy(self.plan)
        active = plan["current"]["active"]
        active["request"]["intent"] = {"action": "dispose"}
        with self.assertRaises(Refusal):
            apply.checked(plan)

    def test_plan_request_artifact_and_build_identity_cannot_drift(self):
        for field, value in (("artifact", dict(self.plan["artifact"], digest="0" * 64)), ("workload", "0" * 64), ("artifact", dict(self.plan["artifact"], build=None))):
            plan = dict(self.plan, **{field: value})
            with self.subTest(field=field), self.assertRaises(Refusal):
                apply.checked(plan)
        plan = copy.deepcopy(self.plan)
        plan["current"]["active"]["request"]["context"]["request"] = "0" * 64
        with self.assertRaises(Refusal):
            apply.checked(plan)

    def test_handoff_manifest_headers_guard_tool_and_workload_tamper_refuses(self):
        for field in ("key", "content", "guard", "implementation", "tool"):
            document = copy.deepcopy(self.document)
            if field in ("key", "content"):
                document[field] = "0" * 64
            elif field == "guard":
                document["basis"]["receipt"]["guard"]["ok"] = False
            elif field == "implementation":
                document["basis"]["receipt"]["implementation"]["lib.media.node"] = "0" * 64
            else:
                document["basis"]["receipt"]["tools"]["node"]["sha256"] = "0" * 64
            with self.subTest(field=field), self.assertRaises(Refusal):
                payload.manifest(self.plan, document, self.bodies.__getitem__)
        name = self.document["files"]["_headers"]["blob"]
        self.bodies[name] = b"/*\n  Content-Security-Policy: default-src *\n"
        with self.assertRaises(Refusal):
            self.multipart()

    def test_cache_key_collision_refuses_before_any_serialized_request(self):
        with mock.patch.object(payload, "digest", return_value="0" * 32), self.assertRaises(Refusal):
            payload.manifest(self.plan, self.document, self.bodies.__getitem__)
        with mock.patch.object(payload, "digest", return_value="0" * 32), self.assertRaises(Refusal):
            self.multipart()

    def test_same_bytes_and_extension_may_share_one_cache_identity(self):
        files = dict(FILES, **{"copy.css": FILES["style.css"]})
        document, bodies = handoff.describe(files, self.receipt)
        held = {hashlib.sha256(body).hexdigest(): body for body in bodies.values()}
        value = operation()
        value["request"]["intent"]["artifact"] = apply.identity(document)
        value["assessment"]["request"] = codec.digest("request", value["request"])
        registered_value = registered(value)
        registered_value["generation"] = self.receipt["source"]["declaration"]
        current = state.started(state.initial(registered_value), {key: value[key] for key in ("request", "assessment")}, registered_value)
        plan = apply.prepare(registered_value, current, document, held.__getitem__)
        manifest = payload.manifest(plan, document, held.__getitem__)
        self.assertEqual(manifest["/copy.css"], manifest["/style.css"])

    def test_unknown_fields_malformed_documents_and_mutable_bodies_refuse(self):
        for plan in (None, {}, dict(self.plan, completion="private"), dict(self.plan, registration={})):
            with self.assertRaises(Refusal):
                apply.checked(plan)
        for document in (None, {}, dict(self.document, files={}), dict(self.document, arbitrary="never")):
            with self.assertRaises(Refusal):
                apply.content(document, self.bodies.__getitem__)
        with self.assertRaises(Refusal):
            apply.content(self.document, lambda name: bytearray(self.bodies[name]))

    def test_plan_detachment_preserves_original_registration_and_reservation(self):
        plan = apply.checked(self.plan)
        plan["registration"]["mapping"]["resource"] = "another"
        plan["current"]["active"]["request"]["context"]["requested"]["app"] = "another"
        self.assertEqual(self.plan["registration"]["mapping"]["resource"], "crest-development")
        self.assertEqual(self.plan["current"]["active"]["request"]["context"]["requested"]["app"], "crest")

    def test_metadata_boundary_collision_refuses_instead_of_rewriting_content(self):
        boundary = "wharf-" + codec.digest("request", self.plan["current"]["active"]["request"])
        with self.assertRaises(Refusal):
            self.multipart(completion="a." + boundary + ".c")


if __name__ == "__main__":
    unittest.main()
