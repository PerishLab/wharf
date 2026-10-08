import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from lib.content import canonical
from lib.content.static import admission
from lib.refusal import Refusal
from scripts import preview
from tests.lib.preview.test_admission import Provider, request, store, CONTEXT

PLATFORM = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REPOSITORY": "PerishLab/wharf",
            "GITHUB_REF": "refs/heads/main", "GITHUB_WORKFLOW_REF": "PerishLab/wharf/.github/workflows/preview.yml@refs/heads/main",
            "GITHUB_WORKFLOW_SHA": "1" * 40, "GITHUB_SHA": "1" * 40, "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}


class Entry(unittest.TestCase):
    def test_strict_request_all_operations_and_foreign_inputs(self):
        for operation in ("apply", "inspect", "discard"):
            selected = request(operation)
            self.assertEqual(admission.requested(json.dumps(selected)), selected)
        invalid = [None, "", "x" * 65537, "\ud800", "{}", json.dumps(dict(request(), repository="foreign/crest")),
                   json.dumps(dict(request(), endpoint="https://evil.example")), '{"schema":"x","schema":"y"}',
                   json.dumps(dict(request(), token="must-not-echo"))]
        for body in invalid:
            with self.subTest(body=repr(body)[:32]), self.assertRaises(Refusal) as observed:
                admission.requested(body)
            self.assertNotIn("must-not-echo", str(observed.exception))

    def test_platform_binds_workflow_and_does_not_read_request_variables(self):
        self.assertEqual(admission.context(PLATFORM), CONTEXT)
        contaminated = dict(PLATFORM, WHARF_RUN="999", WHARF_COMMIT="9" * 40, WHARF_WORKFLOW="foreign", GITHUB_TOKEN="unused")
        self.assertEqual(admission.context(contaminated), CONTEXT)
        for key in PLATFORM:
            missing = dict(PLATFORM)
            del missing[key]
            with self.subTest(key=key), self.assertRaises(Refusal):
                admission.context(missing)

    def test_wrong_platform_events_forks_refs_runs_and_control_sha_refuse(self):
        changes = {"GITHUB_ACTIONS": "false", "GITHUB_EVENT_NAME": "pull_request", "GITHUB_REPOSITORY": "foreign/wharf",
                   "GITHUB_REF": "refs/heads/feature/76", "GITHUB_WORKFLOW_REF": "PerishLab/wharf/.github/workflows/ship.yml@refs/heads/main",
                   "GITHUB_WORKFLOW_SHA": "2" * 40, "GITHUB_SHA": "bad", "GITHUB_RUN_ID": "0", "GITHUB_RUN_ATTEMPT": "-1"}
        for key, value in changes.items():
            with self.subTest(key=key), self.assertRaises(Refusal):
                admission.context(dict(PLATFORM, **{key: value}))
        for value in ("1\n", "01", "1" * 21):
            with self.subTest(value=value), self.assertRaises(Refusal):
                admission.context(dict(PLATFORM, GITHUB_RUN_ID=value))

    def test_observe_uses_separate_credentials_and_existing_provider_checks(self):
        held = dict(PLATFORM, WHARF_PREVIEW_CONTROL_TOKEN="control-reader", WHARF_PREVIEW_PRODUCT_TOKEN="product-reader")
        for operation in ("apply", "inspect", "discard"):
            control, product = Provider(), Provider()
            with mock.patch.object(admission.r2, "registration", return_value=store()) as reader:
                with mock.patch.object(admission, "GitHub", side_effect=[control, product]) as github:
                    observed = admission.observe(request(operation), held)
            self.assertEqual(github.call_args_list, [mock.call("control-reader"), mock.call("product-reader")])
            reader.assert_called_once_with({"repository": "PerishLab/crest", "app": "crest-review"}, held)
            self.assertEqual(control.calls, [("repository", "PerishLab/wharf"), ("run", 123)])
            self.assertFalse(any(call[0] == "run" for call in product.calls))
            self.assertEqual(observed["intent"], canonical.digest(request(operation)))
            self.assertEqual(any(call[0] == "commit" for call in product.calls), operation == "apply")

    def test_foreign_or_missing_platform_refuses_before_reader_factory(self):
        for selected, held in ((dict(request(), repository="foreign/crest"), PLATFORM), (request(), {})):
            with mock.patch.object(admission.r2, "registration") as reader:
                with self.assertRaises(Refusal):
                    admission.observe(selected, held)
            reader.assert_not_called()

    def test_no_generic_token_fallback(self):
        with mock.patch.object(admission.r2, "registration", return_value=store()):
            with self.assertRaises(Refusal):
                admission.observe(request(), dict(PLATFORM, GITHUB_TOKEN="not-a-preview-reader", WHARF_PREVIEW_PRODUCT_TOKEN="product-reader"))

    def test_preflight_outputs_only_safe_selectors(self):
        with tempfile.TemporaryDirectory() as directory:
            output = str(Path(directory) / "output")
            with mock.patch.dict(os.environ, dict(PLATFORM, GITHUB_OUTPUT=output), clear=True):
                result = preview.request({"request": json.dumps(request())})
            self.assertEqual(result["repository"], "PerishLab/crest")
            self.assertEqual(result["operation"], "apply")
            self.assertEqual(set(result), {"owner", "name", "repository", "operation", "intent"})

    def test_admit_outputs_digest_not_target_or_result(self):
        selected = request()
        with tempfile.TemporaryDirectory() as directory:
            output = str(Path(directory) / "output")
            with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": output}, clear=True):
                with mock.patch.object(admission, "observe", return_value={"observed": "trusted"}):
                    result = preview.admit({"request": json.dumps(selected)})
            self.assertEqual(result, {"intent": canonical.digest(selected), "admission": canonical.digest({"observed": "trusted"})})

    def test_cli_refusal_does_not_echo_unknown_credentials_or_call_admission(self):
        body = json.dumps(dict(request(), token="must-not-echo"))
        error, output = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(error), contextlib.redirect_stdout(output):
            with mock.patch.object(admission, "observe") as observe:
                self.assertEqual(preview.main(["admit", "--request", body]), 2)
        observe.assert_not_called()
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn("must-not-echo", error.getvalue())

    def test_component_preflight_before_mint_and_step_scoped_read_credentials(self):
        root = Path(preview.__file__).resolve().parent.parent
        text = (root / ".github/actions/preview-admission/action.yml").read_text()
        self.assertLess(text.index("scripts.preview request"), text.index("actions/create-github-app-token@"))
        self.assertLess(text.index("actions/create-github-app-token@"), text.index("scripts.preview admit"))
        self.assertIn("permission-contents: read", text)
        self.assertIn("repositories: ${{ steps.named.outputs.name }}", text)
        prefix = text.split("    - name: '[verify] request'")[0]
        self.assertNotIn("WHARF_PREVIEW_CONTROL_TOKEN:", prefix)
        self.assertNotIn("WHARF_PREVIEW_REGISTRATION_ACCESS_KEY_ID:", prefix)
        for name in ("CLOUDFLARE", "WHARF_WORKLOAD", "permission-contents: write", "permission-administration:", "actions/checkout@"):
            self.assertNotIn(name, text)
        self.assertEqual(preview.REQUESTED, ("request",))
        self.assertFalse((root / ".github/workflows/preview.yml").exists())


if __name__ == "__main__":
    unittest.main()
