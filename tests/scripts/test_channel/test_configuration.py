import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.media import depot
from lib.media.depot import configuration
from lib.refusal import Refusal
from scripts import channel
from tests.lib.store.memory import Memory

COMMIT = "a" * 40
TREE = "b" * 40


def reader_for(bucket):
    prefix = f"{configuration.BASE}/"

    def read(url):
        if not url.startswith(prefix):
            raise KeyError(url)
        key = url.removeprefix(prefix)
        return bucket.get(key) if bucket.exists(key) else None

    return read


class PlumbConfiguration(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name)
        rules = self.source / "crates/cli/rules"
        (rules / "atoms").mkdir(parents=True)
        (rules / "catalog.toml").write_text('version = 1\n\n[[rule]]\nname = "example"\n')
        (rules / "atoms/limit.toml").write_text("limit = 300\n")
        self.bucket = Memory()

    def scoped(self, marker, repository="PerishLab/plumb"):
        return {
            "repository": repository,
            "marker": marker,
            "commit": COMMIT,
            "tree": TREE,
            "source": str(self.source),
        }

    def test_rc_prepare_stages_rc_channel_without_touching_stable_and_is_idempotent(self):
        first = configuration.prepare(self.scoped("v0.170.0-rc.1"), reader_for(self.bucket), self.bucket)
        self.assertEqual(first["channel"], "rc")
        self.assertEqual(first["version"], "v0.170.0-rc.1")
        self.assertTrue(self.bucket.exists("channels/rc/configurations/latest.json"))
        self.assertFalse(self.bucket.exists("channels/stable/configurations/latest.json"))
        folder = f"channels/rc/configurations/versions/v0.170.0-rc.1/generations/{first['generation']}"
        manifest = json.loads(self.bucket.get(f"{folder}/manifest.json"))
        self.assertEqual([item["path"] for item in manifest["objects"]], ["rules/atoms/limit.toml", "rules/catalog.toml"])
        self.assertTrue(self.bucket.exists(f"{folder}/objects/rules/atoms/limit.toml"))
        self.assertTrue(self.bucket.exists(f"{folder}/objects/rules/catalog.toml"))
        second = configuration.prepare(self.scoped("v0.170.0-rc.1"), reader_for(self.bucket), self.bucket)
        self.assertEqual(second, first)

    def test_stable_prepare_authenticates_staged_rc_against_head_rules_and_refuses_drift(self):
        staged = configuration.prepare(self.scoped("v0.170.0-rc.1"), reader_for(self.bucket), self.bucket)
        authenticated = configuration.prepare(self.scoped("v0.170.0"), reader_for(self.bucket), self.bucket)
        self.assertEqual(authenticated, staged)
        self.assertFalse(self.bucket.exists("channels/stable/configurations/latest.json"))
        self.assertEqual(configuration.verify_planned(self.source, "PerishLab/plumb", authenticated, reader_for(self.bucket)), staged)
        with self.assertRaisesRegex(Refusal, "requires a pinned depot configuration"):
            configuration.verify_planned(self.source, "PerishLab/plumb", None, reader_for(self.bucket))
        (self.source / "crates/cli/rules/catalog.toml").write_text('version = 2\n')
        with self.assertRaisesRegex(Refusal, "differ from HEAD"):
            configuration.prepare(self.scoped("v0.170.0"), reader_for(self.bucket), self.bucket)
        with self.assertRaisesRegex(Refusal, "differ from HEAD"):
            configuration.verify_planned(self.source, "PerishLab/plumb", authenticated, reader_for(self.bucket))

    def test_stable_prepare_refuses_when_rc_channel_configuration_is_absent(self):
        with self.assertRaisesRegex(Refusal, "missing"):
            configuration.prepare(self.scoped("v0.170.0"), reader_for(self.bucket), self.bucket)

    def test_promote_advances_stable_channel_only_after_verifying_rc_and_skips_other_repos(self):
        configuration.prepare(self.scoped("v0.170.0-rc.1"), reader_for(self.bucket), self.bucket)
        self.assertIsNone(configuration.promote(self.source, self.scoped("v0.170.0-rc.1"), reader_for(self.bucket), self.bucket))
        self.assertIsNone(configuration.promote(self.source, self.scoped("v0.170.0", "PerishLab/locus"), reader_for(self.bucket), self.bucket))
        summary = configuration.promote(self.source, self.scoped("v0.170.0"), reader_for(self.bucket), self.bucket)
        self.assertEqual(summary["state"], "published")
        self.assertEqual(summary["discovery"]["state"], "published")
        stable = json.loads(self.bucket.get("channels/stable/configurations/latest.json"))
        self.assertEqual(stable["channel"], "stable")
        self.assertEqual(stable["version"], "v0.170.0")
        self.assertEqual(stable["generation"], summary["generation"])
        stable_folder = f"channels/stable/configurations/versions/v0.170.0/generations/{summary['generation']}"
        stable_manifest = json.loads(self.bucket.get(f"{stable_folder}/manifest.json"))
        self.assertEqual([item["path"] for item in stable_manifest["objects"]], ["rules/atoms/limit.toml", "rules/catalog.toml"])
        self.assertTrue(self.bucket.exists(f"{stable_folder}/objects/rules/atoms/limit.toml"))
        self.assertTrue(self.bucket.exists(f"{stable_folder}/objects/rules/catalog.toml"))
        self.assertEqual(configuration.authenticate(self.source, "stable", reader_for(self.bucket))["generation"], summary["generation"])

    def test_promote_refuses_without_mutating_stable_when_head_rules_differ_from_rc(self):
        configuration.prepare(self.scoped("v0.170.0-rc.1"), reader_for(self.bucket), self.bucket)
        (self.source / "crates/cli/rules/catalog.toml").write_text('version = 9\n')
        with self.assertRaisesRegex(Refusal, "differ from HEAD"):
            configuration.promote(self.source, self.scoped("v0.170.0"), reader_for(self.bucket), self.bucket)
        self.assertFalse(self.bucket.exists("channels/stable/configurations/latest.json"))

    def test_channel_point_promotes_plumb_configuration_before_pointing_release_channel(self):
        order = []
        (self.source / "plumb.toml").write_text('[release]\nbinaries = ["plumb"]\ntargets = ["x86_64-unknown-linux-gnu"]\n')
        releases = mock.Mock()
        releases.get.return_value = json.dumps(
            dict(schema=1, product="plumb", channel="stable", releaseVersion="v0.170.0", commit=COMMIT, artifacts={"linux-x64": {}})
        ).encode()

        def promote(source, context):
            order.append("promote")
            return {"generation": "c" * 64, "state": "published"}

        def point(release, managers, bucket):
            order.append("point")
            return {"pointer": "moved"}

        held = {"repository": "PerishLab/plumb", "marker": "v0.170.0", "wharf": "c" * 40, "source": str(self.source), "planned": "1"}
        with (
            mock.patch.object(channel.publication, "read", return_value=dict(held, commit=COMMIT, tree=TREE)),
            mock.patch.object(channel.r2, "writer", return_value=releases),
            mock.patch.object(channel.depot_configuration, "promote", side_effect=promote),
            mock.patch.object(channel.release, "point", side_effect=point),
        ):
            result = channel.point(held)
        self.assertEqual(order, ["promote", "point"])
        self.assertEqual(result, {"pointer": "moved", "configuration": {"generation": "c" * 64, "state": "published"}})
