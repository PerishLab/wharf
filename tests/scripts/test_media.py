import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from lib import parameters
from lib.refusal import Refusal
from lib.store import plan, workload
from scripts import media
from tests.lib.media import test_images
from tests.lib.media.repository import Repository
from tests.lib.store.memory import Memory

CONTEXT = {"repository": "PerishLab/ensign", "marker": "v0.4.0", "wharf": "c" * 40, "run": "7", "attempt": "1"}
LINUX = "x86_64-unknown-linux-gnu"


class Taken(unittest.TestCase):
    def test_every_parameter_every_action_takes_is_declared(self):
        undeclared = {name for _, names in media.ACTIONS.values() for name in names if name not in parameters.TYPES}
        self.assertEqual(undeclared, set())

    def test_refusal_exits_two(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(media.main(["oci-publish", "--source", "missing"]), 2)
        self.assertIn("media: refused", error.getvalue())


class Imaged(unittest.TestCase):
    def test_an_existing_attachment_only_image_still_plans_public_validation(self):
        repository = Repository({"plumb.toml": '[release.oci]\nregistry = "ghcr.io"\nimage = "perishlab/images"\naccount = "PerishLab"\n'})
        held = {"source": str(repository.root), "repository": "PerishLab/images", "marker": "v0.2.0-rc.1"}
        with mock.patch.object(media.oci, "exists", return_value=True), mock.patch.object(media.parameters, "answer", side_effect=lambda answer: answer):
            planned = media.oci_plan(held)
        self.assertEqual(planned, {"decision": "run", "presence": "present", "image": "ghcr.io/perishlab/images:0.2.0-rc.1"})

    def publish(self, body):
        source = Path(tempfile.mkdtemp())
        (source / "plumb.toml").write_text(body)
        bucket = Memory()
        bound = Path(tempfile.mkdtemp()) / "bound"
        bound.mkdir()
        for name in ("ensign", "ensign-api"):
            (bound / f"{name}-{LINUX}").write_bytes(name.encode())
        workload.publish(bucket, "b" * 64, workload.Produced(bound, {}, {}))
        plan.record(bucket, dict(CONTEXT, commit="a" * 40, tree="d" * 40), {"bind-linux": {"key": "b" * 64, "decision": "run"}, "oci": {"decision": "run"}}, {})
        seen = {}
        admission = mock.Mock()

        def pushed(image, runner, confirm):
            self.assertIs(runner, media.run)
            self.assertTrue(callable(confirm))
            confirm()
            seen.update(binary=Path(image.binary).read_bytes(), name=image.name, reference=image.reference, marker=image.marker)
            return {"state": "published"}

        def point(image, runner, confirm):
            self.assertIs(runner, media.run)
            confirm()
            return None

        with mock.patch.object(media.r2, "configured", return_value=bucket), mock.patch.object(media.oci, "publish", side_effect=pushed), mock.patch.object(media.oci, "advance", side_effect=point) as advanced:
            media.oci_publish(dict(CONTEXT, planned="1", source=str(source), publication=admission))
        self.assertEqual(admission.call_args_list, [mock.call(), mock.call()])
        self.assertEqual(advanced.call_args.args[:2], ("ghcr.io/perishlab/ensign:0.4.0", media.run))
        return seen

    def test_the_image_carries_the_executable_it_names(self):
        seen = self.publish(f'[release]\nbinaries = ["ensign", "ensign-api"]\ntargets = ["{LINUX}"]\n[release.oci]\nbinary = "ensign-api"\n')
        self.assertEqual((seen["name"], seen["binary"]), ("ensign-api", b"ensign-api"))
        self.assertEqual(seen["reference"], "ghcr.io/perishlab/ensign:0.4.0")
        self.assertEqual(seen["marker"], "v0.4.0")

    def test_an_image_naming_none_carries_the_primary(self):
        seen = self.publish(f'[release]\nbinaries = ["ensign-api", "ensign"]\ntargets = ["{LINUX}"]\n[release.oci]\n')
        self.assertEqual((seen["name"], seen["binary"]), ("ensign", b"ensign"))

    def test_an_image_whose_executable_is_not_built_for_linux_refuses(self):
        body = f'[release]\nbinaries = ["ensign", "ensign-api"]\ntargets = ["{LINUX}", "aarch64-apple-darwin"]\n[release.binary.ensign-api]\ntargets = ["aarch64-apple-darwin"]\n[release.oci]\nbinary = "ensign-api"\n'
        with self.assertRaisesRegex(Refusal, "not built for"):
            self.publish(body)

    def test_an_attachment_only_image_fetches_no_binary(self):
        source = Path(tempfile.mkdtemp())
        (source / "plumb.toml").write_text('[release.oci]\nregistry = "ghcr.io"\nimage = "perishlab/images"\naccount = "PerishLab"\n')
        bucket = Memory()
        context = dict(CONTEXT, repository="PerishLab/images")
        plan.record(bucket, dict(context, commit="a" * 40, tree="d" * 40), {"oci": {"decision": "run"}}, {})
        seen = {}
        admission = mock.Mock()

        def pushed(image, runner, confirm):
            self.assertIs(runner, media.run)
            self.assertTrue(callable(confirm))
            confirm()
            seen.update(source=image.source, reference=image.reference)
            return {"state": "published"}

        def point(image, runner, confirm):
            self.assertIs(runner, media.run)
            confirm()
            return {"state": "advanced"}

        with mock.patch.object(media.r2, "configured", return_value=bucket), mock.patch.object(media.oci, "publish_source", side_effect=pushed), mock.patch.object(media.oci, "advance", side_effect=point):
            published = media.oci_publish(dict(context, planned="1", source=str(source), publication=admission))
        self.assertEqual(admission.call_args_list, [mock.call(), mock.call()])
        self.assertEqual(seen, {"source": source, "reference": "ghcr.io/perishlab/images:0.4.0"})
        self.assertEqual(published["channel"], {"state": "advanced"})

    def test_chart_publication_receives_the_explicit_write_admission(self):
        admission = mock.Mock()
        def pushed(source, owner, version, tools):
            self.assertIs(tools.run, media.run)
            tools.confirm()
            return {"version": version}
        with mock.patch.object(media.chart, "publish", side_effect=pushed):
            result = media.chart_publish(dict(CONTEXT, source="/source", publication=admission))
        self.assertEqual(result, {"version": "0.4.0"})
        admission.assert_called_once_with()


class AliasPublication(unittest.TestCase):
    def execute(self, admission, kind="binary"):
        source = Path(self.enterContext(tempfile.TemporaryDirectory()))
        binaries = f'[release]\nbinaries = ["demo"]\ntargets = ["{LINUX}"]\n' if kind != "source" else ""
        (source / "plumb.toml").write_text(binaries + test_images.IMAGE["plumb.toml"])
        bucket = Memory()
        entries = {"oci": {"decision": "run"}}
        if kind != "source":
            bound = source / "bound"
            bound.mkdir()
            (bound / f"demo-{LINUX}").write_bytes(b"binary")
            workload.publish(bucket, "b" * 64, workload.Produced(bound, {}, {}))
            entries["bind-linux"] = {"key": "b" * 64, "decision": "run"}
        context = dict(CONTEXT, repository="PerishLab/demo")
        plan.record(bucket, dict(context, commit="a" * 40, tree="d" * 40), entries, {})
        runner, commands, digests = test_images.Channel().registry("0.3.9")
        original_publish, original_advance = media.oci.publish, media.oci.advance

        def publish(image, selected, confirm):
            if kind == "new":
                confirm()
                return {"state": "published"}
            return original_publish(image, selected, confirm)

        def advance(image, selected=runner, confirm=None):
            self.assertIs(selected, runner)
            return original_advance(image, runner, confirm)

        with mock.patch.object(media.r2, "configured", return_value=bucket), mock.patch.object(media, "run", runner), mock.patch.object(media.oci, "exists", return_value=True), mock.patch.object(media.oci, "publish", side_effect=publish), mock.patch.object(media.oci, "advance", side_effect=advance):
            try:
                result = media.oci_publish(dict(context, planned="1", source=str(source), publication=admission))
            except Refusal as failure:
                result = failure
        return result, commands, digests

    def test_an_existing_binary_image_still_admits_before_the_alias(self):
        admission = mock.Mock()
        result, commands, digests = self.execute(admission)
        admission.assert_called_once_with()
        self.assertEqual(result["state"], "already-published")
        self.assertEqual(result["channel"]["state"], "advanced")
        self.assertEqual(digests[test_images.Channel.TAG], "sha256:" + "4" * 64)
        self.assertEqual(len(test_images.Channel().created(commands)), 1)

    def test_an_existing_binary_image_refuses_before_the_alias(self):
        admission = mock.Mock(side_effect=Refusal("publication inputs changed"))
        result, commands, _ = self.execute(admission)
        self.assertIsInstance(result, Refusal)
        admission.assert_called_once_with()
        self.assertEqual(test_images.Channel().created(commands), [])

    def test_an_existing_source_image_rechecks_before_the_alias(self):
        admission = mock.Mock()
        result, commands, _ = self.execute(admission, "source")
        self.assertEqual(admission.call_count, 3)
        self.assertEqual(result["state"], "already-published")
        self.assertEqual(len(test_images.Channel().created(commands)), 1)

    def test_source_changes_after_image_readback_stop_the_alias(self):
        admission = mock.Mock(side_effect=[None, None, Refusal("late source change")])
        result, commands, _ = self.execute(admission, "source")
        self.assertIsInstance(result, Refusal)
        self.assertEqual(admission.call_count, 3)
        self.assertEqual(test_images.Channel().created(commands), [])

    def test_source_changes_after_a_push_stop_the_alias(self):
        admission = mock.Mock(side_effect=[None, Refusal("late source change")])
        result, commands, _ = self.execute(admission, "new")
        self.assertIsInstance(result, Refusal)
        self.assertEqual(admission.call_count, 2)
        self.assertEqual(test_images.Channel().created(commands), [])
