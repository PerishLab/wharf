import json
import os
import tempfile
import unittest
from pathlib import Path

from unittest import mock

from lib import parameters
from scripts import channel

HELD = {"repository": "PerishLab/plumb", "marker": "v0.38.0", "wharf": "c" * 40}


class Taken(unittest.TestCase):
    def test_every_parameter_every_action_takes_is_declared(self):
        undeclared = {name for _, names in channel.ACTIONS.values() for name in names if name not in parameters.TYPES}
        self.assertEqual(undeclared, set())


class Plan(unittest.TestCase):
    def decide(self, overtaken):
        output = Path(tempfile.mkdtemp()) / "output"
        with mock.patch.dict(os.environ, {parameters.OUTPUT: str(output)}), mock.patch.object(channel.release, "overtaken", return_value=overtaken), mock.patch.object(channel.release, "carried", return_value=True):
            held = channel.plan(dict(HELD, source="../product"))
        return held, output.read_text()

    def test_a_product_with_no_binaries_has_no_channel_to_point(self):
        output = Path(tempfile.mkdtemp()) / "output"
        with mock.patch.dict(os.environ, {parameters.OUTPUT: str(output)}), mock.patch.object(channel.release, "carried", return_value=False):
            held = channel.plan(dict(HELD, source="../product"))
        self.assertEqual(held, {"decision": "skip", "presence": "none", "channel": "stable"})

    def test_a_channel_not_yet_at_a_newer_marker_is_pointed(self):
        self.assertEqual(self.decide(False), ({"decision": "run", "presence": "present", "channel": "stable"}, "decision=run\npresence=present\n"))

    def test_a_channel_already_at_a_newer_marker_is_left_alone(self):
        self.assertEqual(self.decide(True)[1], "decision=skip\npresence=overtaken\n")


class Point(unittest.TestCase):
    def test_the_pointer_is_handed_managers_rendered_for_its_marker(self):
        seen = {}

        def point(release, managers, bucket):
            seen.update(marker=release.marker, wharf=release.wharf, files=sorted(path.name for path in Path(managers).rglob("*") if path.is_file()))
            return {"pointer": "moved"}

        bucket = mock.Mock()
        bucket.get.return_value = json.dumps({"artifacts": {key: {} for key in ("linux-x64", "darwin-arm64", "windows-x64")}}).encode()
        source = Path(tempfile.mkdtemp())
        (source / "plumb.toml").write_text('[release]\nbinaries = ["plumb"]\ntargets = ["x86_64-unknown-linux-gnu", "aarch64-apple-darwin", "x86_64-pc-windows-msvc"]\n')
        with mock.patch.object(channel.r2, "writer", return_value=bucket), mock.patch.object(channel.release, "point", side_effect=point):
            self.assertEqual(channel.point(dict(HELD, source=str(source))), {"pointer": "moved"})
        self.assertEqual(seen, {"marker": "v0.38.0", "wharf": "c" * 40, "files": ["manage.ps1", "manage.ps1", "manage.sh", "manage.sh"]})

    def test_the_canonical_managers_install_what_the_product_declares_for_each_sealed_platform(self):
        rendered = {}
        source = Path(tempfile.mkdtemp())
        (source / "plumb.toml").write_text('[release]\nbinaries = ["santi", "santi-api"]\ntargets = ["x86_64-unknown-linux-gnu", "aarch64-apple-darwin"]\n[release.binary.santi-api]\ninstall = false\n')
        bucket = mock.Mock()
        bucket.get.return_value = json.dumps({"artifacts": {"linux-x64": {}, "linux-x64-deb": {}}}).encode()
        with mock.patch.object(channel.r2, "writer", return_value=bucket), mock.patch.object(channel.release, "render", side_effect=lambda release, output, installed: rendered.update(installed)), mock.patch.object(channel.release, "point", return_value={}):
            channel.point(dict(HELD, repository="PerishLab/santi", source=str(source)))
        self.assertEqual(rendered, {"x86_64-unknown-linux-gnu": ["santi"]})
