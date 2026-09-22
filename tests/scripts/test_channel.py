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
        with mock.patch.dict(os.environ, {parameters.OUTPUT: str(output)}), mock.patch.object(channel.release, "overtaken", return_value=overtaken):
            held = channel.plan(HELD)
        return held, output.read_text()

    def test_a_channel_not_yet_at_a_newer_marker_is_pointed(self):
        self.assertEqual(self.decide(False), ({"decision": "run", "channel": "stable"}, "decision=run\n"))

    def test_a_channel_already_at_a_newer_marker_is_left_alone(self):
        self.assertEqual(self.decide(True)[1], "decision=skip\n")


class Point(unittest.TestCase):
    def test_the_pointer_is_handed_managers_rendered_for_its_marker(self):
        seen = {}

        def point(release, managers, bucket):
            seen.update(marker=release.marker, wharf=release.wharf, files=sorted(path.name for path in Path(managers).rglob("*") if path.is_file()))
            return {"pointer": "moved"}

        with mock.patch.object(channel.r2, "writer", return_value="bucket"), mock.patch.object(channel.release, "point", side_effect=point):
            self.assertEqual(channel.point(HELD), {"pointer": "moved"})
        self.assertEqual(seen, {"marker": "v0.38.0", "wharf": "c" * 40, "files": ["manage.ps1", "manage.ps1", "manage.sh", "manage.sh"]})
