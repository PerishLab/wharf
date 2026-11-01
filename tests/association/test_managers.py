import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.content import implementation
from lib.process.association import managers
from lib.refusal import Refusal
from tests.association.world import World


class Managers(unittest.TestCase):
    def test_unrelated_publisher_edit_does_not_change_renderer_identity(self):
        original = implementation.source
        before = managers.renderer()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "release.py"
            path.write_text(original("lib.media.release").read_text() + "\ndef unrelated():\n    return 'new publisher'\n")
            with mock.patch.object(implementation, "source", side_effect=lambda name: path if name == "lib.media.release" else original(name)):
                self.assertEqual(managers.renderer(), before)
            path.write_text(path.read_text().replace('default_version": held', 'default_version": "changed"'))
            with mock.patch.object(implementation, "source", side_effect=lambda name: path if name == "lib.media.release" else original(name)):
                self.assertNotEqual(managers.renderer(), before)

    def test_original_generator_is_provenance_and_actual_script_bytes_are_proved(self):
        world = World()
        publication = {"source": world.source, "publisher": world.publisher, "seal": world.seal}
        publication["seal"]["generator"]["template"] = "d" * 64
        with world.wired():
            result = managers.verify(publication, world.model)
        self.assertEqual(result["generator"]["template"], "d" * 64)
        self.assertEqual(result["implementation"], managers.renderer())
        changed = copy.deepcopy(publication)
        changed["seal"]["generator"]["origin"]["commit"] = "e" * 40
        with world.wired(), self.assertRaises(Refusal):
            managers.verify(changed, world.model)
