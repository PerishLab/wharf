import copy
import unittest
from unittest import mock

from lib.content import canonical
from lib.process.trial import preparation
from lib.refusal import Refusal
from tests.scripts.test_distribution.world import World


class Frozen(unittest.TestCase):
    def test_exact_world_and_groups_pass(self):
        world = World()
        with world.wired():
            preparation.unchanged(world.prepared)
        self.assertEqual(preparation.completed(world.prepared, world.report(), world.execution(), world.installed()), canonical.digest(world.prepared))

    def test_runner_product_and_scene_movement_refuse(self):
        world = World()
        for name in ("product", "runner", "scene", "image"):
            with self.subTest(name=name), world.wired(), mock.patch.object(preparation, name, return_value={"moved": True}), self.assertRaises(Refusal):
                preparation.unchanged(world.prepared)

    def test_missing_or_widened_coverage_refuses(self):
        world = World()
        mutations = [{"platform": "darwin-aarch64"}, {"scene": dict(world.scene, generation="0" * 64)}, {"groups": []}, {"groups": [{"digest": "c" * 64, "cases": []}]}, {"groups": [{"digest": "c" * 64, "cases": ["writer", "writer"]}]}, {"images": []}, {"images": [{"name": "plumb", "reference": "tag", "digest": "mutable:latest"}]}]
        for change in mutations:
            with self.subTest(change=change), self.assertRaises(Refusal):
                preparation.completed(world.prepared, dict(world.report(), **change), world.execution(), world.installed())
        with self.assertRaises(Refusal):
            preparation.completed(world.prepared, world.report(), dict(world.execution(), cases=[]), world.installed())
        with self.assertRaises(Refusal):
            preparation.completed(world.prepared, world.report(), world.execution(), dict(world.installed(), pointer={}))

    def test_diagnostic_is_frozen_separately_and_unknown_policy_refuses(self):
        world = World()
        with world.wired():
            value = preparation.freeze(world.admitted, world.prepared["observer"], "fault", world.prepared["started"])
        self.assertEqual(value["requirements"]["selected"], ["fault"])
        self.assertEqual(value["requirements"]["scope"], "diagnostic")
        for field, value in (("policy", "old"), ("schema", "unknown"), ("requirements", dict(world.prepared["requirements"], selected=[]))):
            with self.subTest(field=field), self.assertRaises(Refusal):
                preparation.validate_prepared(dict(world.prepared, **{field: value}))
        value = copy.deepcopy(world.prepared)
        value["requirements"]["declared"] = ["other"]
        with self.assertRaises(Refusal):
            preparation.validate_prepared(value)

    def test_runtime_image_follows_declared_channel_or_product_release(self):
        world = World()
        with world.wired(), mock.patch.object(preparation.oci, "public_digest", return_value="sha256:" + "b" * 64) as resolve:
            self.assertEqual(preparation.image(world.admitted), world.prepared["image"])
            self.assertEqual(resolve.call_args.args[0], "ghcr.io/perishlab/plumb:1.2.3-rc.1")
            admitted = copy.deepcopy(world.admitted)
            admitted["trial"]["specification"]["image"] = "ghcr.io/perishlab/images:stable"
            self.assertEqual(preparation.image(admitted)["reference"], "ghcr.io/perishlab/images:stable")
            self.assertEqual(resolve.call_args.args[0], "ghcr.io/perishlab/images:stable")
