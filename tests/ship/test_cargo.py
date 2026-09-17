import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from wharf.refusal import Refusal
from wharf.ship import binary, cargo
from wharf.store import workload

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
        inputs = cargo.resolve(self.root, "demo", "x86_64-unknown-linux-gnu", "ubuntu-24.04")
        return workload.key(inputs, workload.implementation(binary, cargo))


class Key(unittest.TestCase):
    def setUp(self):
        self.repository = Repository()
        self.base = self.repository.key()

    def changed(self, path, old, new):
        self.repository.edit(path, old, new)
        self.repository.commit()
        return self.repository.key() != self.base

    def test_resolves_entry_group_from_convention(self):
        inputs = cargo.resolve(self.repository.root, "demo", "x86_64-unknown-linux-gnu", "ubuntu-24.04")
        self.assertEqual(inputs["entry"]["package"], "demo-cli")
        self.assertEqual(sorted(inputs["members"]), ["crates/cli", "crates/lib"])
        self.assertEqual([entry[0] for entry in inputs["lock"]], ["demo", "demo-cli", "itoa"])

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
            cargo.resolve(self.repository.root, "missing", "x86_64-unknown-linux-gnu", "ubuntu-24.04")
