import os
import subprocess
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
        self.root = Path(self.directory.name) / "owned"

    def test_source_and_state_addresses_stay_in_one_owned_run(self):
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

    def test_repository_names_do_not_collide_with_ownership_files(self):
        first = operation.seat(self.root, SUMMARY)
        first.mkdir()
        second = operation.seat(self.root, dict(SUMMARY, repository="PerishLab/example.json", repository_id=18))
        self.assertNotEqual(first, second)
        self.assertTrue((self.root / "ownership/PerishLab/example.json.json").is_file())


class Qualification(unittest.TestCase):
    def test_guard_configuration_does_not_need_reader_environment_inheritance(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            child = operation.environment(home, home / "owned", SUMMARY["repository"], {"PATH": "/bin", "WHARF_FOLLOW_TOKEN": "app", "WHARF_PACKAGES_TOKEN": "fixture-reader"})
            child.pop("WHARF_PACKAGES_TOKEN")
            config = Path(child["NPM_CONFIG_USERCONFIG"])
            self.assertEqual(config.read_text(), "//npm.pkg.github.com/:_authToken=fixture-reader\n")
            self.assertEqual(config.stat().st_mode & 0o077, 0)

    def test_reader_line_injection_refuses_before_writing_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            for reader in ("private\nregistry=https://elsewhere/", "private\rnext"):
                with self.assertRaises(Refusal) as refused:
                    operation.environment(home, home / "owned", SUMMARY["repository"], {"WHARF_FOLLOW_TOKEN": "app", "WHARF_PACKAGES_TOKEN": reader})
                self.assertNotIn("private", str(refused.exception))
                self.assertFalse((home / "npmrc").exists())

    def test_absent_reader_creates_no_credential_file(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            child = operation.environment(home, home / "owned", SUMMARY["repository"], {"WHARF_FOLLOW_TOKEN": "app"})
            self.assertNotIn("NPM_CONFIG_USERCONFIG", child)
            self.assertFalse((home / "npmrc").exists())

    def test_detached_git_retains_only_the_qualified_origin_header(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            held = {"PATH": os.environ["PATH"], "WHARF_FOLLOW_TOKEN": "app", "GIT_CONFIG_COUNT": "99"}
            child = operation.environment(home, home / "owned", SUMMARY["repository"], held)
            child.pop("GIT_CONFIG_COUNT", None)
            route = "https://github.com/PerishLab/example.git"
            lookup = ["git", "config", "--get-urlmatch", "http.extraHeader"]
            bound = subprocess.run([*lookup, route + "/info/refs"], cwd=home, env=child, check=True, capture_output=True, text=True)
            self.assertEqual(bound.stdout.strip(), "Authorization: Basic eC1hY2Nlc3MtdG9rZW46YXBw")
            for url in (route + "-neighbor", route.replace("https", "http"), route.replace("github.com", "elsewhere.example")):
                foreign = subprocess.run([*lookup, url], cwd=home, env=child, capture_output=True, text=True)
                self.assertEqual((foreign.returncode, foreign.stdout), (1, ""))
            redirects = subprocess.run(["git", "config", "--get", "http.followRedirects"], cwd=home, env=child, check=True, capture_output=True, text=True)
            self.assertEqual(redirects.stdout.strip(), "false")
            self.assertEqual(Path(child["GIT_CONFIG_GLOBAL"]).stat().st_mode & 0o777, 0o600)

    def test_missing_root_declaration_is_a_no_op(self):
        replies = iter([
            {"id": 17, "full_name": "PerishLab/example", "default_branch": "main", "archived": False},
            {"object": {"sha": "a" * 40}},
            None,
        ])
        self.assertFalse(operation.governed(SUMMARY, "fixture", lambda *args: next(replies)))

    def test_api_maps_404_optional_refusal_and_unknown_failures(self):
        not_found = subprocess.CalledProcessError(1, ["runseal"], output=b'{"error":{"outcome":"refused","evidence":{"status":404}}}')
        forbidden = subprocess.CalledProcessError(1, ["runseal"], output=b'{"error":{"outcome":"refused","evidence":{"status":403}}}')
        unknown = subprocess.CalledProcessError(1, ["runseal"], output=b'{"error":{"outcome":"unknown","evidence":{"status":null}}}')
        with mock.patch.object(operation.process, "github", side_effect=not_found):
            self.assertIsNone(operation.api(["contents", "PerishLab", "example", "a" * 40, "plumb.toml"], "t", True))
        with mock.patch.object(operation.process, "github", side_effect=forbidden), self.assertRaisesRegex(Refusal, "refused by GitHub"):
            operation.api(["repository", "PerishLab", "example"], "t")
        with mock.patch.object(operation.process, "github", side_effect=unknown), self.assertRaisesRegex(Refusal, "is unknown"):
            operation.api(["repository", "PerishLab", "example"], "t")

    def test_other_repository_identity_refuses(self):
        with self.assertRaisesRegex(Refusal, "repository identity"):
            operation.governed(SUMMARY, "fixture", lambda *args: {"id": 18})

    def test_wrong_installation_runs_no_command_or_workspace_write(self):
        with mock.patch.object(operation.event, "qualified", return_value=SUMMARY), mock.patch.object(operation, "seat") as seat, mock.patch.object(operation, "command") as command:
            with self.assertRaisesRegex(Refusal, "another installation"):
                operation.execute("fixture", {"WHARF_FOLLOW_TOKEN": "fixture", "WHARF_FOLLOW_INSTALLATION_ID": "24"})
        seat.assert_not_called()
        command.assert_not_called()

    def test_child_holds_only_the_app_token_and_package_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            env = {"PATH": "/bin", "WHARF_FOLLOW_TOKEN": "app", "WHARF_PACKAGES_TOKEN": "reader", "WHARF_FOLLOW_WEBHOOK_SECRET": "webhook", "WHARF_FOLLOW_DISPATCH_TOKEN": "dispatch", "WHARF_FOLLOW_APP_PRIVATE_KEY": "key", "GITHUB_TOKEN": "job", "WHARF_R2_SECRET_ACCESS_KEY": "writer"}
            child = operation.environment(home, home / "owned", SUMMARY["repository"], env)
            self.assertEqual({name: value for name, value in child.items() if name.endswith(("TOKEN", "SECRET", "PRIVATE_KEY", "SECRET_ACCESS_KEY"))}, {"GITHUB_TOKEN": "app", "WHARF_PACKAGES_TOKEN": "reader"})
            self.assertEqual(child["PLUMB_HOME"], str(home / "owned/plumb"))
            self.assertEqual(Path(child["NPM_CONFIG_USERCONFIG"]).stat().st_mode & 0o077, 0)


class Lifetime(unittest.TestCase):
    def test_each_invocation_uses_and_removes_independent_state(self):
        held = []
        def command(argv, cwd, env):
            self.assertNotIn("gh", argv)
            if argv[0] == "plumb":
                self.assertTrue(Path(cwd).is_dir())
                root = Path(env["PLUMB_HOME"])
                if argv[1:3] == ["configuration", "install"]:
                    self.assertFalse(root.exists())
                    root.mkdir()
                else:
                    self.assertEqual(argv[1], "follow")
                    self.assertTrue(root.is_dir())
                    (root / "state.json").write_text("owned invocation")
                    held.append((root, Path(cwd)))
        env = {"WHARF_FOLLOW_TOKEN": "fixture", "WHARF_FOLLOW_INSTALLATION_ID": "23", "PATH": "/bin"}
        with mock.patch.object(operation.event, "qualified", return_value=SUMMARY), mock.patch.object(operation, "governed", return_value=True), mock.patch.object(operation, "synchronize", side_effect=lambda source, *args: source.mkdir()), mock.patch.object(operation, "command", side_effect=command):
            for _ in range(2):
                self.assertEqual(operation.execute("fixture", env)["state"], "executed")
        self.assertNotEqual(held[0][0], held[1][0])
        self.assertTrue(all(not path.exists() for entry in held for path in entry))

    def test_refused_operation_removes_only_its_disposable_state(self):
        held = []
        def command(argv, cwd, env):
            held.append(Path(env["PLUMB_HOME"]).parent)
            raise Refusal("fixture stop")
        env = {"WHARF_FOLLOW_TOKEN": "fixture", "WHARF_FOLLOW_INSTALLATION_ID": "23", "PATH": "/bin"}
        with mock.patch.object(operation.event, "qualified", return_value=SUMMARY), mock.patch.object(operation, "governed", return_value=True), mock.patch.object(operation, "command", side_effect=command):
            with self.assertRaisesRegex(Refusal, "fixture stop"):
                operation.execute("fixture", env)
        self.assertEqual(len(held), 1)
        self.assertFalse(held[0].exists())
