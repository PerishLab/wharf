import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from lib.content import implementation
from lib.cargo import basis
from lib.refusal import Refusal
from lib.store import workload

FILES = {
    "Cargo.toml": """
        [workspace]
        members = ["crates/*"]

        [workspace.package]
        version = "0.0.0"
    """,
    "rust-toolchain.toml": """
        [toolchain]
        channel = "1.96.1"
    """,
    "crates/cli/Cargo.toml": """
        [package]
        name = "demo-cli"
        version.workspace = true

        [[bin]]
        name = "demo"
        path = "src/main.rs"

        [dependencies]
        demo = { path = "../lib", version = "=0.0.0" }
    """,
    "crates/cli/src/main.rs": "fn main() {}\n",
    "crates/lib/Cargo.toml": """
        [package]
        name = "demo"
        version.workspace = true

        [dependencies]
        itoa = "1"
    """,
    "crates/lib/src/lib.rs": "// lib\n",
    "crates/other/Cargo.toml": """
        [package]
        name = "other"
        version.workspace = true

        [dependencies]
        ryu = "1"
    """,
    "crates/other/src/lib.rs": "// other\n",
    "Cargo.lock": """
        version = 4

        [[package]]
        name = "demo"
        version = "0.0.0"
        dependencies = ["itoa"]

        [[package]]
        name = "demo-cli"
        version = "0.0.0"
        dependencies = ["demo"]

        [[package]]
        name = "itoa"
        version = "1.0.0"
        source = "registry+https://github.com/rust-lang/crates.io-index"
        checksum = "aaaa"

        [[package]]
        name = "other"
        version = "0.0.0"
        dependencies = ["ryu"]

        [[package]]
        name = "ryu"
        version = "1.0.0"
        source = "registry+https://github.com/rust-lang/crates.io-index"
        checksum = "bbbb"
    """,
    "docs/readme.md": "hello\n",
}


class Repository:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp())
        self.git("init", "-q")
        for path, body in FILES.items():
            self.write(path, textwrap.dedent(body).lstrip())
        self.commit()

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True)

    def write(self, path, body):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)

    def edit(self, path, old, new):
        target = self.root / path
        text = target.read_text()
        assert old in text, (path, old)
        target.write_text(text.replace(old, new))

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", "change")

    def key(self):
        held = basis.resolve(self.root, "demo", "x86_64-unknown-linux-gnu", "ubuntu-24.04")
        return workload.key(held, implementation.digest("lib.cargo.basis", "lib.cargo.build"))


class Key(unittest.TestCase):
    def setUp(self):
        self.repository = Repository()
        self.base = self.repository.key()

    def changed(self, path, old, new):
        self.repository.edit(path, old, new)
        self.repository.commit()
        return self.repository.key() != self.base

    def test_resolves_entry_group_from_convention(self):
        held = basis.resolve(self.repository.root, "demo", "x86_64-unknown-linux-gnu", "ubuntu-24.04")
        self.assertEqual(held["entry"]["package"], "demo-cli")
        self.assertEqual(sorted(held["members"]), ["crates/cli", "crates/lib"])
        self.assertEqual([entry[0] for entry in held["lock"]], ["demo", "demo-cli", "itoa"])

    def test_key_is_stable(self):
        self.assertEqual(self.repository.key(), self.base)

    def test_reached_source_changes_the_key(self):
        self.assertTrue(self.changed("crates/lib/src/lib.rs", "// lib", "pub fn f() {}"))

    def test_toolchain_changes_the_key(self):
        self.assertTrue(self.changed("rust-toolchain.toml", "1.96.1", "1.96.2"))

    def test_reached_lock_entry_changes_the_key(self):
        self.assertTrue(self.changed("Cargo.lock", 'checksum = "aaaa"', 'checksum = "cccc"'))

    def test_unreached_member_does_not_change_the_key(self):
        self.assertFalse(self.changed("crates/other/src/lib.rs", "// other", "pub fn g() {}"))

    def test_unreached_lock_entry_does_not_change_the_key(self):
        self.assertFalse(self.changed("Cargo.lock", 'checksum = "bbbb"', 'checksum = "dddd"'))

    def test_unrelated_file_does_not_change_the_key(self):
        self.assertFalse(self.changed("docs/readme.md", "hello", "bye"))

    def test_uncommitted_edits_do_not_count(self):
        self.repository.edit("crates/lib/src/lib.rs", "// lib", "pub fn h() {}")
        self.assertEqual(self.repository.key(), self.base)

    def test_refuses_a_declared_version(self):
        self.repository.edit("Cargo.toml", 'version = "0.0.0"', 'version = "1.2.3"')
        self.repository.commit()
        with self.assertRaises(Refusal):
            self.repository.key()

    def test_refuses_a_floating_toolchain(self):
        self.repository.edit("rust-toolchain.toml", '"1.96.1"', '"stable"')
        self.repository.commit()
        with self.assertRaises(Refusal):
            self.repository.key()

    def test_refuses_an_unknown_binary(self):
        with self.assertRaises(Refusal):
            basis.resolve(self.repository.root, "missing", "x86_64-unknown-linux-gnu", "ubuntu-24.04")


class SuiteBasis(unittest.TestCase):
    def setUp(self):
        self.repository = Repository()

    def held(self):
        return basis.suite(self.repository.root, "ubuntu-24.04")

    def test_follows_the_whole_tree(self):
        before = self.held()
        self.assertEqual(before["entry"]["kind"], "cargo-suite")
        self.repository.edit("docs/readme.md", "hello", "bye")
        self.repository.commit()
        self.assertNotEqual(self.held()["tree"], before["tree"])

    def test_refuses_declared_versions(self):
        self.repository.edit("Cargo.toml", 'version = "0.0.0"', 'version = "1.2.3"')
        self.repository.commit()
        with self.assertRaises(Refusal):
            self.held()


class Dependencies(unittest.TestCase):
    def setUp(self):
        self.repository = Repository()

    def held(self):
        return basis.dependencies(self.repository.root, "demo", "x86_64-unknown-linux-gnu", "ubuntu-24.04")

    def key(self):
        return workload.key(self.held(), implementation.digest("lib.cargo.basis", "lib.cargo.build"))

    def test_a_member_is_taken_as_its_manifest_not_its_tree(self):
        self.assertEqual(sorted(self.held()["members"]), ["crates/cli/Cargo.toml", "crates/lib/Cargo.toml"])

    def test_a_source_change_leaves_the_dependencies_where_they_are(self):
        before = self.key()
        self.repository.write("crates/lib/src/lib.rs", "// changed\n")
        self.repository.commit()
        self.assertNotEqual(self.repository.key(), before, "the binary must follow its own sources")
        self.assertEqual(self.key(), before)

    def test_a_declared_dependency_change_moves_them(self):
        before = self.key()
        self.repository.edit("crates/lib/Cargo.toml", 'itoa = "1"', 'itoa = "1.0.0"')
        self.repository.commit()
        self.assertNotEqual(self.key(), before)

    def test_a_locked_version_change_moves_them(self):
        before = self.key()
        self.repository.edit("Cargo.lock", 'checksum = "aaaa"', 'checksum = "cccc"')
        self.repository.commit()
        self.assertNotEqual(self.key(), before)

    def test_the_target_and_the_runner_are_part_of_what_is_built(self):
        self.assertNotEqual(self.key(), workload.key(basis.dependencies(self.repository.root, "demo", "aarch64-apple-darwin", "macos-15"), implementation.digest("lib.cargo.basis", "lib.cargo.build")))

    def test_it_is_not_the_binary_under_another_name(self):
        self.assertNotEqual(self.key(), self.repository.key())
