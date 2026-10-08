import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.process.follow import operation
from lib.refusal import Refusal

SUMMARY = {"repository": "PerishLab/example", "repository_id": 17, "installation_id": 23, "after": "a" * 40, "delivery": "fixture"}


class Workspace(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "persistent"

    def test_source_and_state_addresses_remain_stable(self):
        first = operation.seat(self.root, SUMMARY)
        second = operation.seat(self.root, SUMMARY)
        self.assertEqual(first, second)
        self.assertEqual(first, self.root / "repositories/PerishLab/example")

    def test_recreated_repository_refuses_without_changing_ownership(self):
        operation.seat(self.root, SUMMARY)
        marker = self.root / "ownership/PerishLab/example.json"
        original = marker.read_bytes()
        with self.assertRaisesRegex(Refusal, "ownership disagrees"):
            operation.seat(self.root, dict(SUMMARY, repository_id=18))
        self.assertEqual(marker.read_bytes(), original)

    def test_foreign_nonempty_root_is_preserved(self):
        self.root.mkdir()
        payload = self.root / "valuable"
        payload.write_text("kept")
        with self.assertRaisesRegex(Refusal, "not empty or owned"):
            operation.seat(self.root, SUMMARY)
        self.assertEqual(payload.read_text(), "kept")
        self.assertFalse((self.root / ".wharf-follow.json").exists())

    def test_existing_unowned_source_is_preserved(self):
        source = operation.seat(self.root, SUMMARY)
        (self.root / "ownership/PerishLab/example.json").unlink()
        source.mkdir()
        (source / "kept").write_text("valuable")
        with self.assertRaisesRegex(Refusal, "without ownership"):
            operation.seat(self.root, SUMMARY)
        self.assertEqual((source / "kept").read_text(), "valuable")

    def test_path_aliases_refuse_before_writing_outside_the_root(self):
        operation.seat(self.root, SUMMARY)
        external = Path(self.directory.name) / "external"
        external.mkdir()
        (self.root / "plumb").symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(Refusal, "aliases"):
            operation.seat(self.root, SUMMARY)
        self.assertEqual(list(external.iterdir()), [])

    def test_disposable_or_relative_data_paths_refuse(self):
        for env in ({"WHARF_FOLLOW_DATA_ROOT": "relative"}, {"WHARF_FOLLOW_DATA_ROOT": str(self.root), "RUNNER_TEMP": self.directory.name}):
            with self.subTest(env=env), self.assertRaises(Refusal):
                operation.directory(env)

    def test_repository_names_do_not_collide_with_ownership_files(self):
        first = operation.seat(self.root, SUMMARY)
        first.mkdir()
        second = operation.seat(self.root, dict(SUMMARY, repository="PerishLab/example.json", repository_id=18))
        self.assertNotEqual(first, second)
        self.assertTrue((self.root / "ownership/PerishLab/example.json.json").is_file())


class Qualification(unittest.TestCase):
    def test_missing_root_declaration_is_a_no_op(self):
        replies = iter([{"id": 17, "full_name": "PerishLab/example", "default_branch": "main", "archived": False}, None])
        self.assertFalse(operation.governed(SUMMARY, "fixture", lambda *args: next(replies)))

    def test_other_repository_identity_refuses(self):
        with self.assertRaisesRegex(Refusal, "repository identity"):
            operation.governed(SUMMARY, "fixture", lambda *args: {"id": 18})

    def test_wrong_installation_runs_no_command_or_workspace_write(self):
        with mock.patch.object(operation.event, "qualified", return_value=SUMMARY), mock.patch.object(operation, "seat") as seat, mock.patch.object(operation, "command") as command:
            with self.assertRaisesRegex(Refusal, "another installation"):
                operation.execute("fixture", {"GH_TOKEN": "fixture", "WHARF_FOLLOW_INSTALLATION_ID": "24"})
        seat.assert_not_called()
        command.assert_not_called()

    def test_child_holds_only_the_app_token_and_package_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            env = {"PATH": "/bin", "GH_TOKEN": "app", "WHARF_PACKAGES_TOKEN": "reader", "WHARF_FOLLOW_WEBHOOK_SECRET": "webhook", "WHARF_FOLLOW_DISPATCH_TOKEN": "dispatch", "WHARF_FOLLOW_APP_PRIVATE_KEY": "key", "GITHUB_TOKEN": "job", "WHARF_R2_SECRET_ACCESS_KEY": "writer"}
            child = operation.environment(home, home / "persistent", env)
            self.assertEqual({name: value for name, value in child.items() if name.endswith(("TOKEN", "SECRET", "PRIVATE_KEY", "SECRET_ACCESS_KEY"))}, {"GH_TOKEN": "app", "WHARF_PACKAGES_TOKEN": "reader"})
            self.assertEqual(child["PLUMB_HOME"], str(home / "persistent/plumb"))
            self.assertEqual(Path(child["NPM_CONFIG_USERCONFIG"]).stat().st_mode & 0o077, 0)
