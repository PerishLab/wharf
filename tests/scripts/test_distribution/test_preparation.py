import copy
import unittest
from unittest import mock

from lib.content import canonical
from lib.process.trial import consumer, declaration, preparation, publication
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


class Services(unittest.TestCase):
    def test_executor_rechecks_public_service_material_without_native_identity(self):
        world = World().service()
        snapshot = world.prepared["services"]["locus"]
        authority = snapshot["declaration"]["authority"]
        pointer = authority + "/v1/channels/stable.json"
        archive = snapshot["seal"]["artifacts"]["linux-x64-deb"]["url"]
        with world.combined(), mock.patch.object(preparation, "native", side_effect=Refusal("no executor identity")):
            preparation.execution(world.prepared)
            with self.assertRaises(Refusal):
                preparation.unchanged(world.prepared)
        changes = [(pointer, canonical.encode(dict(snapshot["pointer"], commit="0" * 40))), (snapshot["pointer"]["seal"]["url"], canonical.encode(dict(snapshot["seal"], commit="0" * 40))), (authority + "/v1/releases/stable/v0.16.0/distribution.json", canonical.encode(dict(snapshot["distribution"], state="incomplete"))), (archive, b"corrupt")]
        for address, body in changes:
            original = world.documents[address]
            world.documents[address] = body
            with self.subTest(address=address), world.combined(), mock.patch.object(preparation, "native", side_effect=Refusal("no executor identity")), self.assertRaises(Refusal):
                preparation.execution(world.prepared)
            world.documents[address] = original

    def test_exact_service_world_and_package_are_frozen(self):
        world = World().service()
        with world.combined():
            preparation.unchanged(world.prepared)
        self.assertEqual(world.prepared["schema"], "wharf.windtun.prepared/v2")
        self.assertEqual(set(world.prepared["services"]), {"locus"})
        self.assertEqual(preparation.completed(world.prepared, world.report(), world.execution(), world.installed()), canonical.digest(world.prepared))
        snapshot = world.prepared["services"]["locus"]
        self.assertEqual(snapshot["source"]["commit"], world.peer["commit"])
        self.assertEqual(snapshot["tag"], "9" * 40)
        self.assertEqual(snapshot["repository"]["id"], 42)

    def test_unknown_nonstable_self_and_unsafe_services_refuse(self):
        world = World().service()
        base = world.specification["services"]["locus"]
        for mutation in [{"channel": "rc"}, {"authority": "https://foreign.invalid"}, {"pin": "v0.16.0"}, {"placement": dict(base["placement"], artifact="linux-x64")}, {"placement": dict(base["placement"], paths={"locus-api": "../outside"})}, {"placement": dict(base["placement"], paths={"locus-api": "usr/bin/locus-api", "extra": "usr/bin/extra"})}]:
            with self.subTest(mutation=mutation), self.assertRaises(Refusal):
                declaration.validate(dict(world.specification, services={"locus": dict(base, **mutation)}))
        for peers in [{}, {str(index): base for index in range(5)}]:
            with self.assertRaises(Refusal):
                declaration.validate(dict(world.specification, services=peers))
        with self.assertRaises(Refusal):
            preparation.services(world.specification, "locus")

    def test_service_product_set_and_placement_are_exact(self):
        world = World().service()
        products = world.report()["products"]
        for value in [products[:1], products + [world.peer], products + [dict(world.peer, name="foreign")], [dict(world.peer, placement={}), products[1]], [dict(world.peer, archive="0" * 64), products[1]], [dict(world.peer, extra="field"), products[1]]]:
            with self.subTest(products=value), self.assertRaises(Refusal):
                preparation.completed(world.prepared, dict(world.report(), products=value), world.execution(), world.installed())

    def test_service_native_source_and_current_movement_refuse(self):
        world = World().service()
        world.native["repos/PerishLab/locus/git/commits/" + "6" * 40] = {"sha": "6" * 40, "tree": {"sha": "8" * 40}}
        for key, field, value in [("repos/PerishLab/locus", "archived", True), ("repos/PerishLab/locus/git/ref/tags/v0.16.0", "object", {"type": "commit", "sha": "9" * 40}), ("repos/PerishLab/locus/git/tags/" + "9" * 40, "object", {"type": "commit", "sha": "6" * 40})]:
            original = copy.deepcopy(world.native[key])
            world.native[key][field] = value
            with world.combined(), self.assertRaises(Refusal):
                preparation.unchanged(world.prepared)
            world.native[key] = original
        with world.combined(), mock.patch.object(preparation, "service", return_value={"moved": True}), self.assertRaises(Refusal):
            preparation.unchanged(world.prepared)

    def test_bounded_package_bytes_and_config_placement(self):
        import tomllib
        world = World().service()
        snapshot = world.prepared["services"]["locus"]
        artifact = snapshot["seal"]["artifacts"]["linux-x64-deb"]
        with world.combined():
            publication.material(artifact, snapshot["declaration"]["authority"])
            for value in [dict(artifact, size=64 * 1024 * 1024 + 1), dict(artifact, size=1), dict(artifact, url="https://foreign.invalid/package.deb"), dict(artifact, sha256="0" * 64)]:
                with self.assertRaises(Refusal):
                    publication.material(value, snapshot["declaration"]["authority"])
        text = consumer.service_text("locus", snapshot["declaration"], world.specification["image"])
        parsed = tomllib.loads(text)["locus"]
        self.assertEqual(parsed["placement"], world.peer["placement"])
        self.assertEqual(parsed["image"], world.specification["image"])
        self.assertEqual(parsed["channel"], "stable")
