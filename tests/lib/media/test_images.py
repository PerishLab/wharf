import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from lib.media import chart, oci
from lib.refusal import Refusal
from tests.lib.media.repository import Repository

IMAGE = {"plumb.toml": '[release.oci]\nregistry = "ghcr.io"\nimage = "perishlab/demo"\naccount = "PerishLab"\n'}
CHART = {"plumb.toml": '[release.chart]\nregistry = "ghcr.io"\nchart = "perishlab/demo"\naccount = "PerishLab"\n'}
WEB_PKG = json.dumps({"name": "@demo/web", "private": True, "scripts": {"build": "vite build"}}) + "\n"
MULTI_TOML = '[release]\nbinaries = ["ensign-api"]\ntargets = ["x86_64-unknown-linux-gnu"]\n[release.oci]\nregistry = "ghcr.io"\naccount = "PerishLab"\n[release.oci.components.api]\nimage = "perishlab/ensign"\nbinary = "ensign-api"\n[release.oci.components.web]\nimage = "perishlab/ensign-web"\nweb = "apps/web"\nfile = "deploy/web.Dockerfile"\n'


def web_repo(toml=MULTI_TOML, extra=None):
    return Repository({"plumb.toml": toml, "apps/web/package.json": WEB_PKG, "deploy/web.Dockerfile": "FROM node:24-slim\nCOPY dist /app/dist\n", **(extra or {})})


class Oci(unittest.TestCase):
    def test_reference_is_owner_scoped_and_lowercase(self):
        self.assertEqual(oci.reference("PerishLab/plumb", "1.2.3-beta.4"), "ghcr.io/perishlab/plumb:1.2.3-beta.4")
    def test_an_image_is_carried_only_where_plumb_declares_one(self):
        self.assertFalse(oci.carried(Repository().root))
        self.assertTrue(oci.carried(Repository(IMAGE).root))
    def test_context_holds_the_containerfile_and_named_binary(self):
        repository = Repository(IMAGE)
        binary = repository.root / "built"
        binary.write_bytes(b"elf")
        self.assertEqual(sorted(p.name for p in Path(oci.context(repository.root, binary, "demo")).iterdir()), ["Containerfile", "demo"])
    def test_source_context_holds_every_tracked_file_and_no_untracked_file(self):
        repository = Repository(IMAGE)
        repository.write("untracked", "outside\n")
        repository.write("Containerfile", "FROM changed\n")
        directory = oci.source_context(repository.root)
        self.assertEqual((directory / "Containerfile").read_text(), "FROM scratch\nCOPY demo /demo\n")
        self.assertTrue((directory / "packages/lib/package.json").is_file() and not (directory / "untracked").exists())
    def test_refuses_without_a_tracked_containerfile(self):
        repository = Repository(IMAGE)
        repository.git("rm", "-q", "Containerfile")
        repository.commit()
        with self.assertRaises(Refusal):
            oci.context(repository.root, repository.root / "missing", "demo")


class Smoked(unittest.TestCase):
    def publish(self, answer, check=None):
        repository = Repository(IMAGE)
        binary = repository.root / "built"
        binary.write_bytes(b"elf")
        request = oci.Image(repository.root, binary, "demo", "ghcr.io/perishlab/demo:0.4.0-rc.1", "v0.4.0-rc.1")
        commands = []
        def confirm():
            commands.append(["admit"])
            if check:
                check()
        def runner(argv, cwd):
            commands.append(argv[:2])
            if argv[:2] == ["docker", "run"]:
                self.assertEqual(argv[2:], ["--rm", "--platform", "linux/amd64", request.reference, "--version"])
                return answer()
            return '["ghcr.io/perishlab/demo@sha256:0"]' if argv[:3] == ["docker", "image", "inspect"] else ""
        with mock.patch.object(oci, "exists", side_effect=[False, True]):
            try:
                return oci.publish(request, runner, confirm), commands
            except Refusal as refusal:
                return refusal, commands
    def test_pushes_only_after_the_image_reports_its_marker(self):
        result, commands = self.publish(lambda: "demo v0.4.0-rc.1\n")
        self.assertEqual((result["state"], commands), ("published", [["docker", "build"], ["docker", "run"], ["admit"], ["docker", "push"], ["docker", "image"]]))
    def test_refuses_changed_publication_inputs_after_smoke_before_push(self):
        result, commands = self.publish(lambda: "demo v0.4.0-rc.1", lambda: (_ for _ in ()).throw(Refusal("latest package or source changed")))
        self.assertTrue(isinstance(result, Refusal) and commands == [["docker", "build"], ["docker", "run"], ["admit"]])
    def test_refuses_an_image_whose_executable_does_not_start(self):
        result, commands = self.publish(lambda: (_ for _ in ()).throw(subprocess.CalledProcessError(1, "docker", "", "demo: version `GLIBC_2.39' not found")))
        self.assertTrue(isinstance(result, Refusal) and "ghcr.io/perishlab/demo:0.4.0-rc.1 --version exited 1" in str(result) and "GLIBC_2.39" in str(result))
        self.assertNotIn(["docker", "push"], commands)
    def test_refuses_an_image_reporting_another_line(self):
        result, commands = self.publish(lambda: "demo v0.3.0\n")
        self.assertTrue(isinstance(result, Refusal) and "ghcr.io/perishlab/demo:0.4.0-rc.1 --version reported 'demo v0.3.0'" in str(result) and "expected 'demo v0.4.0-rc.1'" in str(result))
        self.assertNotIn(["docker", "push"], commands)


class PublishedSource(unittest.TestCase):
    def test_source_image_builds_without_running_an_executable_and_proves_its_public_digest(self):
        request, commands = oci.SourceImage(Repository(IMAGE).root, "ghcr.io/perishlab/demo:0.4.0"), []
        def runner(argv, cwd):
            commands.append(argv[:2])
            return '["ghcr.io/perishlab/demo@sha256:' + "0" * 64 + '"]' if argv[:3] == ["docker", "image", "inspect"] else ""
        with mock.patch.object(oci, "exists", return_value=False), mock.patch.object(oci, "public_digest", return_value="sha256:" + "0" * 64):
            result = oci.publish_source(request, runner, lambda: commands.append(["admit"]))
        self.assertEqual((result["digests"], commands), (["ghcr.io/perishlab/demo@sha256:" + "0" * 64], [["docker", "build"], ["admit"], ["docker", "push"], ["docker", "image"]]))
    def test_source_admission_failure_writes_no_image_and_removes_context(self):
        request, calls, directories = oci.SourceImage(Repository(IMAGE).root, "ghcr.io/perishlab/demo:0.4.0"), [], []
        def runner(argv, cwd):
            calls.append(argv[:2])
            directories.append(Path(cwd))
            return ""
        with mock.patch.object(oci, "exists", return_value=False), self.assertRaisesRegex(Refusal, "stable control changed"):
            oci.publish_source(request, runner, lambda: (_ for _ in ()).throw(Refusal("stable control changed")))
        self.assertEqual(calls, [["docker", "build"]])
        self.assertFalse(any(directory.exists() for directory in directories))
    def test_public_digest_uses_an_empty_docker_configuration(self):
        seen = {}
        def runner(argv, cwd):
            seen.update(argv=argv, cwd=cwd)
            return json.dumps("sha256:" + "1" * 64)
        self.assertEqual(oci.public_digest("ghcr.io/perishlab/demo:0.4.0", runner), "sha256:" + "1" * 64)
        self.assertEqual(seen["argv"][:4], ["docker", "--config", str(seen["cwd"]), "buildx"])
    def test_an_existing_source_image_is_reused_only_after_its_public_digest_matches(self):
        request, digest, confirm = oci.SourceImage(Repository(IMAGE).root, "ghcr.io/perishlab/demo:0.4.0"), "sha256:" + "2" * 64, mock.Mock()
        with mock.patch.object(oci, "exists", return_value=True), mock.patch.object(oci, "registry_digest", return_value=digest), mock.patch.object(oci, "public_digest", return_value=digest):
            result = oci.publish_source(request, confirm=confirm)
        self.assertEqual((result, confirm.call_count), ({"image": request.reference, "state": "already-published", "digests": [f"ghcr.io/perishlab/demo@{digest}"]}, 2))
    def test_an_existing_private_source_image_is_not_reused(self):
        request = oci.SourceImage(Repository(IMAGE).root, "ghcr.io/perishlab/demo:0.4.0")
        with mock.patch.object(oci, "exists", return_value=True), mock.patch.object(oci, "registry_digest", return_value="sha256:" + "2" * 64), mock.patch.object(oci, "public_digest", side_effect=Refusal("not public")), self.assertRaisesRegex(Refusal, "not public"):
            oci.publish_source(request)
    def test_an_existing_source_image_with_another_public_digest_is_not_reused(self):
        request = oci.SourceImage(Repository(IMAGE).root, "ghcr.io/perishlab/demo:0.4.0")
        with mock.patch.object(oci, "exists", return_value=True), mock.patch.object(oci, "registry_digest", return_value="sha256:" + "2" * 64), mock.patch.object(oci, "public_digest", return_value="sha256:" + "3" * 64), self.assertRaisesRegex(Refusal, "publicly resolved"):
            oci.publish_source(request)


class Channel(unittest.TestCase):
    IMAGE, TAG = "ghcr.io/perishlab/demo:0.4.0", "ghcr.io/perishlab/demo:stable"
    def registry(self, labelled=None, error=None, digests=None):
        digests, commands = dict({self.IMAGE: "sha256:" + "4" * 64}, **(digests or {})), []
        def runner(argv, cwd):
            commands.append(argv)
            if argv[:2] == ["skopeo", "copy"]:
                digests[argv[-1].removeprefix("docker://")] = argv[-2].rsplit("@", 1)[1]
                return ""
            reference = argv[-1]
            if argv[-2] == "{{json .Image}}":
                if error or labelled is None:
                    raise subprocess.CalledProcessError(1, argv, "", error or f"ERROR: {reference}: not found")
                return json.dumps({"config": {"Labels": {oci.LABEL: labelled}}})
            return json.dumps(digests[reference])
        return runner, commands, digests
    def created(self, commands):
        return [argv for argv in commands if argv[:2] == ["skopeo", "copy"]]
    def test_a_release_candidate_never_moves_the_channel(self):
        runner, commands, _ = self.registry()
        self.assertEqual((oci.advance("ghcr.io/perishlab/demo:0.4.0-rc.1", runner), oci.current("ghcr.io/perishlab/demo:0.4.0-rc.1", runner), commands), (None, True, []))
    def test_an_absent_channel_points_at_the_version_digest(self):
        runner, commands, digests = self.registry()
        self.assertFalse(oci.current(self.IMAGE, runner))
        self.assertEqual((oci.advance(self.IMAGE, runner)["state"], digests[self.TAG]), ("advanced", "sha256:" + "4" * 64))
        self.assertEqual(self.created(commands)[0][2:], ["--preserve-digests", "docker://ghcr.io/perishlab/demo@sha256:" + "4" * 64, f"docker://{self.TAG}"])
    def test_an_older_stable_on_the_channel_is_replaced(self):
        runner, commands, _ = self.registry("0.3.9")
        self.assertEqual((oci.current(self.IMAGE, runner), oci.advance(self.IMAGE, runner)["state"], len(self.created(commands))), (False, "advanced", 1))
    def test_a_later_stable_on_the_channel_is_kept(self):
        runner, commands, _ = self.registry("0.4.1")
        self.assertEqual((oci.current(self.IMAGE, runner), oci.advance(self.IMAGE, runner), self.created(commands)), (True, {"image": self.TAG, "state": "kept", "version": "0.4.1"}, []))
    def test_the_same_stable_on_the_same_digest_is_kept(self):
        runner, commands, _ = self.registry("0.4.0", digests={self.TAG: "sha256:" + "4" * 64})
        self.assertEqual((oci.advance(self.IMAGE, runner)["state"], self.created(commands)), ("kept", []))
    def test_the_same_stable_on_another_digest_is_repointed(self):
        runner, commands, digests = self.registry("0.4.0", digests={self.TAG: "sha256:" + "5" * 64})
        self.assertEqual((oci.advance(self.IMAGE, runner)["state"], digests[self.TAG]), ("advanced", "sha256:" + "4" * 64))
    def test_an_unreadable_channel_refuses_instead_of_moving(self):
        runner, commands, _ = self.registry(error="ERROR: unexpected status 503")
        with self.assertRaisesRegex(Refusal, "cannot be read"):
            oci.advance(self.IMAGE, runner)
        self.assertEqual(self.created(commands), [])
    def test_the_channel_is_read_and_verified_with_the_registry_login(self):
        runner, commands, _ = self.registry("0.3.9")
        oci.advance(self.IMAGE, runner)
        self.assertTrue(commands and not [argv for argv in commands if "--config" in argv])
    def test_a_channel_without_a_release_version_refuses(self):
        runner, commands, _ = self.registry("latest")
        with self.assertRaisesRegex(Refusal, "no release version"):
            oci.advance(self.IMAGE, runner)
        self.assertEqual(self.created(commands), [])
    def test_admission_immediately_precedes_the_alias_copy(self):
        runner, commands, _ = self.registry("0.3.9")
        result = oci.advance(self.IMAGE, runner, lambda: commands.append(["admit"]))
        copy = commands.index(self.created(commands)[0])
        self.assertEqual((commands[copy - 1], commands.count(["admit"]), result["state"]), (["admit"], 1, "advanced"))
    def test_admission_failure_starts_no_alias_copy(self):
        runner, commands, _ = self.registry("0.3.9")
        confirm = mock.Mock(side_effect=Refusal("publication inputs changed"))
        with self.assertRaisesRegex(Refusal, "publication inputs changed"):
            oci.advance(self.IMAGE, runner, confirm)
        confirm.assert_called_once_with()
        self.assertEqual(self.created(commands), [])
    def test_nonstable_and_kept_aliases_need_no_additional_admission(self):
        for image, version, digests in ((self.IMAGE + "-rc.1", None, {}), (self.IMAGE, "0.4.1", {}), (self.IMAGE, "0.4.0", {self.TAG: "sha256:" + "4" * 64})):
            runner, commands, _ = self.registry(version, digests=digests)
            confirm = mock.Mock(side_effect=AssertionError("unnecessary admission"))
            with self.subTest(image=image, version=version):
                oci.advance(image, runner, confirm)
                confirm.assert_not_called()
                self.assertEqual(self.created(commands), [])


class MultiOci(unittest.TestCase):
    def test_resolves_api_binary_and_web_components(self):
        repo = web_repo()
        self.assertTrue(oci.carried(repo.root))
        items = oci.components(repo.root, "PerishLab/ensign", "0.4.0")
        self.assertEqual([(c.name, c.kind, c.reference, c.file, c.binary, c.web, c.package, c.multi) for c in items], [("api", "binary", "ghcr.io/perishlab/ensign:0.4.0", "Containerfile", "ensign-api", None, None, True), ("web", "web", "ghcr.io/perishlab/ensign-web:0.4.0", "deploy/web.Dockerfile", None, "apps/web", "@demo/web", True)])
    def test_component_without_binary_or_web_stays_source_without_primary_fallback(self):
        toml = '[release]\nbinaries = ["ensign-api"]\ntargets = ["x86_64-unknown-linux-gnu"]\n[release.oci.components.doc]\nimage = "perishlab/ensign-doc"\n'
        items = oci.components(web_repo(toml).root, "PerishLab/ensign", "0.4.0")
        self.assertEqual((items[0].kind, items[0].binary), ("source", None))
    def test_rejects_invalid_component_declarations(self):
        cases = (("dup", '[release.oci.components.a]\nimage = "perishlab/x"\n[release.oci.components.b]\nimage = "perishlab/x"\n', {}), ("mix", '[release.oci]\nimage = "perishlab/x"\n[release.oci.components.a]\nimage = "perishlab/x"\n', {}), ("name", '[release.oci.components.Bad_Name]\nimage = "perishlab/x"\n', {}), ("no-file", '[release.oci.components.web]\nimage = "perishlab/w"\nweb = "apps/web"\n', {}), ("no-build", '[release.oci.components.web]\nimage = "perishlab/w"\nweb = "apps/web"\nfile = "deploy/web.Dockerfile"\n', {"apps/web/package.json": '{"name":"@demo/web"}\n'}), ("trav", '[release.oci.components.web]\nimage = "perishlab/w"\nweb = "apps/web"\nfile = "../escape"\n', {}), ("owner", '[release.oci.components.api]\nimage = "other/ensign"\n', {}), ("top", '[release.oci]\nregistry = "ghcr.io"\nimage = "perishlab/x"\nextra = 1\n', {}), ("comp", '[release.oci.components.api]\nimage = "perishlab/x"\nextra = 1\n', {}), ("both", '[release.oci.components.api]\nimage = "perishlab/x"\nbinary = "a"\nweb = "apps/web"\nfile = "deploy/web.Dockerfile"\n', {}))
        for label, toml, extra in cases:
            with self.subTest(label=label), self.assertRaises(Refusal):
                oci.components(web_repo(toml, extra).root, "PerishLab/ensign", "0.4.0")
    def test_rejects_symlink_containerfile(self):
        repo = web_repo('[release.oci]\nimage = "perishlab/ensign"\nfile = "link.Dockerfile"\n')
        os.symlink("Containerfile", repo.root / "link.Dockerfile")
        repo.commit()
        with self.assertRaises(Refusal):
            oci.carried(repo.root)


class PublishedWeb(unittest.TestCase):
    def test_publish_web_isolates_credentials_builds_clean_context_and_smokes(self):
        repo, digest, ref = web_repo(), "sha256:" + "a" * 64, "ghcr.io/perishlab/ensign-web:0.4.0"
        stale = repo.root / "apps/web/dist/stale.js"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("stale\n")
        request = oci.WebImage(repo.root, "apps/web", "@demo/web", "deploy/web.Dockerfile", ref, env={"HOME": "/ambient", "PATH": os.environ.get("PATH", ""), "WHARF_PACKAGES_TOKEN": "tok-123", "WHARF_R2_SECRET_ACCESS_KEY": "r2-sec"})
        seen, auth_files = [], []
        def runner(argv, cwd, env=None):
            seen.append((argv, env))
            if argv[0] == "pnpm" and "config" in argv:
                self.assertFalse(any(k in env for k in ("NPM_CONFIG_USERCONFIG", "WHARF_PACKAGES_TOKEN", "WHARF_R2_SECRET_ACCESS_KEY")))
                return '["svelte@5.57.1"]\n'
            if argv[:2] == ["pnpm", "install"]:
                cfg = Path(env["NPM_CONFIG_USERCONFIG"])
                self.assertTrue(cfg.is_file() and env.get("CI") == "true" and env.get("HOME") != "/ambient" and "WHARF_R2_SECRET_ACCESS_KEY" not in env and not stale.exists())
                auth_files.append((cfg, env["HOME"]))
            elif argv[:3] == ["pnpm", "--filter", "@demo/web"]:
                self.assertTrue(env.get("CI") == "true" and env.get("HOME") == auth_files[0][1])
                self.assertFalse(auth_files[0][0].exists() or any(k in env for k in ("NPM_CONFIG_USERCONFIG", "WHARF_PACKAGES_TOKEN", "WHARF_R2_SECRET_ACCESS_KEY")))
                server = repo.root / "apps/web/dist/.perish/server.mjs"
                server.parent.mkdir(parents=True, exist_ok=True)
                server.write_text("export default 1;\n")
            elif argv[:2] == ["docker", "build"]:
                self.assertEqual(sorted(p.name for p in Path(cwd).iterdir()), ["Containerfile", "dist"])
                self.assertFalse(any((Path(cwd) / p).exists() for p in (".npmrc", "node_modules", "dist/stale.js")))
            elif argv[:3] == ["docker", "image", "inspect"]:
                return json.dumps({oci.LABEL: "0.4.0"} if "Labels" in argv[4] else [f"ghcr.io/perishlab/ensign-web@{digest}"])
            return ""
        with mock.patch.object(oci, "exists", return_value=False), mock.patch.object(oci, "registry_digest", return_value=digest), mock.patch.object(oci, "public_digest", return_value=digest):
            result = oci.publish_web(request, runner, lambda: seen.append((["admit"], None)))
        self.assertEqual((result, seen[1][0], [argv[:2] for argv, _ in seen[2:]]), ({"image": ref, "digest": digest, "status": "published"}, ["pnpm", "install", "--frozen-lockfile", "--config.minimum-release-age-exclude=svelte@5.57.1", "--config.minimum-release-age-exclude=@perishlab/*"], [["pnpm", "--filter"], ["docker", "build"], ["docker", "image"], ["docker", "run"], ["admit"], ["docker", "push"], ["docker", "image"]]))
        self.assertEqual(seen[5][0][2:], ["--rm", "--platform", "linux/amd64", "--entrypoint", "node", ref, "--check", "dist/.perish/server.mjs"])
    def test_publish_web_refuses_symlink_or_tracked_dist_before_pnpm(self):
        ref = "ghcr.io/perishlab/ensign-web:0.4.0"
        repo, runner = web_repo(), mock.Mock(side_effect=AssertionError("pnpm ran"))
        os.symlink("/tmp", repo.root / "apps/web/dist")
        with mock.patch.object(oci, "exists", return_value=False), self.assertRaisesRegex(Refusal, "symlink"):
            oci.publish_web(oci.WebImage(repo.root, "apps/web", "@demo/web", "deploy/web.Dockerfile", ref, env={}), runner)
        tracked_repo = web_repo(extra={"apps/web/dist/old.js": "x\n"})
        with mock.patch.object(oci, "exists", return_value=False), self.assertRaisesRegex(Refusal, "tracked"):
            oci.publish_web(oci.WebImage(tracked_repo.root, "apps/web", "@demo/web", "deploy/web.Dockerfile", ref, env={}), runner)
        runner.assert_not_called()
    def test_publish_web_refuses_when_smoke_fails_before_push(self):
        repo, pushed = web_repo(), []
        request = oci.WebImage(repo.root, "apps/web", "@demo/web", "deploy/web.Dockerfile", "ghcr.io/perishlab/ensign-web:0.4.0", env={})
        def runner(argv, cwd, env=None):
            if argv[:2] == ["pnpm", "--filter"]:
                (repo.root / "apps/web/dist").mkdir(parents=True, exist_ok=True)
                (repo.root / "apps/web/dist/index.html").write_text("ok")
            elif argv[:3] == ["docker", "image", "inspect"]:
                return json.dumps({oci.LABEL: "0.3.0"})
            elif argv[:2] == ["docker", "push"]:
                pushed.append(argv)
            return ""
        with mock.patch.object(oci, "exists", return_value=False), self.assertRaisesRegex(Refusal, "expected '0.4.0'"):
            oci.publish_web(request, runner)
        self.assertEqual(pushed, [])
    def test_publish_web_existing_verifies_public_digest(self):
        digest, ref = "sha256:" + "b" * 64, "ghcr.io/perishlab/ensign-web:0.4.0"
        request = oci.WebImage(web_repo().root, "apps/web", "@demo/web", "deploy/web.Dockerfile", ref, env={})
        with mock.patch.object(oci, "exists", return_value=True), mock.patch.object(oci, "registry_digest", return_value=digest), mock.patch.object(oci, "public_digest", return_value=digest):
            self.assertEqual(oci.publish_web(request), {"image": ref, "digest": digest, "status": "already-published"})


class Chart(unittest.TestCase):
    def test_lists_the_declared_chart_alone(self):
        self.assertEqual(chart.charts(Repository().root), [])
        extra = {"charts/other/Chart.yaml": "name: other\nversion: 0.0.0\n"}
        self.assertEqual(chart.charts(Repository(dict(CHART, **extra)).root), [{"name": "demo", "path": "charts/demo"}])
        self.assertEqual(chart.repository("PerishLab"), "oci://ghcr.io/perishlab/charts")
    def test_refuses_declared_chart_versions(self):
        with self.assertRaises(Refusal):
            chart.charts(Repository(dict(CHART, **{"charts/demo/Chart.yaml": "name: demo\nversion: 1.0.0\n"})).root)
    def test_refuses_a_declared_chart_the_tree_does_not_carry(self):
        with self.assertRaisesRegex(Refusal, "which no charts/\\*/Chart.yaml names"):
            chart.charts(Repository({"plumb.toml": CHART["plumb.toml"].replace("perishlab/demo", "perishlab/gone")}).root)
