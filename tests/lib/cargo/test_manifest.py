import unittest

from lib.cargo import manifest
from tests.lib.cargo.test_basis import Repository


class Virtual(unittest.TestCase):
    def setUp(self):
        self.repository = Repository()

    def changed(self, old, new):
        self.repository.edit("Cargo.toml", old, new)
        self.repository.commit()
        return manifest.virtual(self.repository.root)

    def test_a_root_without_a_package_or_default_members_is_virtual(self):
        self.assertTrue(manifest.virtual(self.repository.root))

    def test_a_root_package_is_not_virtual(self):
        self.assertFalse(self.changed("[workspace]\n", '[package]\nname = "root"\n\n[workspace]\n'))

    def test_default_members_narrow_what_a_plain_cargo_test_runs(self):
        self.assertFalse(self.changed('members = ["crates/*"]\n', 'members = ["crates/*"]\ndefault-members = ["crates/cli"]\n'))
