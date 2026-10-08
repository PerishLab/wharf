import contextlib
import io
import unittest
from pathlib import Path
from unittest import mock

from lib.content import resources
from lib.refusal import Refusal
from scripts import preview


class SourceComponent(unittest.TestCase):
    def test_named_literal_identity_before_token_mint(self):
        held = {"repository": "PerishLab/crest", "commit": "a" * 40, "tree": "b" * 40}
        with mock.patch.object(preview.parameters, "answer", side_effect=lambda value: value):
            self.assertEqual(preview.named(held), {"owner": "PerishLab", "name": "crest", "commit": "a" * 40, "tree": "b" * 40})
            for changed in ({"repository": "attacker/crest"}, {"repository": "PerishLab/../crest"}, {"commit": "main"}, {"commit": "refs/tags/v1.0.0"}, {"tree": ""}):
                with self.subTest(changed=changed), self.assertRaises(Refusal):
                    preview.named(dict(held, **changed))

    def test_cli_reports_refusal_and_does_not_run_checkout(self):
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()), mock.patch.object(preview.workspace, "acquired") as acquired:
            self.assertEqual(preview.main(["named", "--repository", "PerishLab/crest", "--commit", "main", "--tree", "b" * 40]), 2)
            acquired.assert_not_called()

    def test_component_pins_scoped_actions_and_exact_source_only_settings(self):
        root = Path(preview.__file__).resolve().parent.parent
        text = (root / ".github/actions/preview-source/action.yml").read_text()
        actions = resources.read_json("check/actions.json")
        self.assertIn("actions/checkout@", text)
        self.assertIn("WHARF_SOURCE: ${{ github.workspace }}/preview-product", text)
        self.assertIn("actions/create-github-app-token@", text)
        for setting in ("permission-contents: read", "repositories: ${{ steps.named.outputs.name }}", "ref: ${{ steps.named.outputs.commit }}", "fetch-depth: '0'", "submodules: 'false'", "lfs: 'false'", "persist-credentials: 'false'", "set-safe-directory: 'false'", "GIT_TEMPLATE_DIR: ''", "GIT_CONFIG_GLOBAL: /dev/null", "run: python3 -B -m scripts.preview source"):
            self.assertIn(setting, text)
        self.assertLess(text.index("scripts.preview named"), text.index("actions/create-github-app-token@"))
        self.assertLess(text.index("actions/checkout@"), text.index("scripts.preview source"))
        self.assertNotIn("refs/tags/", text)
        self.assertNotIn("CLOUDFLARE", text)
        self.assertNotIn("R2_", text)
        self.assertTrue(actions)

    def test_release_product_checkout_remains_tag_only(self):
        root = Path(preview.__file__).resolve().parent.parent
        text = (root / ".github/actions/product/action.yml").read_text()
        self.assertIn("ref: refs/tags/${{ inputs.marker }}", text)
        self.assertNotIn("inputs.commit", text)


if __name__ == "__main__":
    unittest.main()
