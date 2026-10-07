import json
import subprocess
import unittest

from lib.cargo import toolchain
from lib.refusal import Refusal

DOMAIN = {"node.version": "24.18.0", "pnpm.version": "11.13.0", "rust.version": "1.96.1"}


class Resolve(unittest.TestCase):
    def test_reads_plumb_metadata_once(self):
        calls = []

        def runner(argv, cwd):
            calls.append(argv)
            return json.dumps(dict(DOMAIN, extra="kept out")) + "\n"

        self.assertEqual(toolchain.resolve(runner), DOMAIN)
        self.assertEqual(calls, [["plumb", "metadata", "--json"]])

    def test_refuses_an_unreadable_or_inexact_answer(self):
        def missing(argv, cwd):
            raise FileNotFoundError("plumb")

        def failed(argv, cwd):
            raise subprocess.CalledProcessError(2, argv)

        for runner in (missing, failed, lambda argv, cwd: "not json", lambda argv, cwd: json.dumps(dict(DOMAIN, **{"rust.version": "stable"}))):
            with self.subTest(runner), self.assertRaises(Refusal):
                toolchain.resolve(runner)


class Versions(unittest.TestCase):
    def test_reads_the_run_resolution(self):
        self.assertEqual(toolchain.versions({toolchain.VARIABLE: toolchain.encoded(DOMAIN)}), DOMAIN)

    def test_refuses_without_it(self):
        for env in ({}, {toolchain.VARIABLE: ""}, {toolchain.VARIABLE: "{"}, {toolchain.VARIABLE: json.dumps({"rust.version": "1.96.1"})}):
            with self.subTest(env), self.assertRaises(Refusal):
                toolchain.versions(env)

    def test_the_rust_toolchain_is_the_domain_version_at_minimal_profile(self):
        self.assertEqual(toolchain.current(), {"channel": "1.96.1", "profile": "minimal", "components": [], "targets": []})
