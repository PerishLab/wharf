import base64
import unittest

from lib.check.locking import records
from lib.refusal import Refusal

INTEGRITY = "sha512-" + base64.b64encode(bytes(64)).decode()


class RecordTests(unittest.TestCase):
    def test_canonical_public_and_private_locations(self):
        self.assertEqual(records.location("demo@1.2.3", {"integrity": INTEGRITY}), "https://registry.npmjs.org/demo/-/demo-1.2.3.tgz")
        self.assertEqual(records.location("@scope/demo@1.2.3", {"integrity": INTEGRITY}), "https://registry.npmjs.org/@scope/demo/-/demo-1.2.3.tgz")
        url = "https://npm.pkg.github.com/download/@perishlab/demo/1.2.3/" + "a" * 40
        self.assertEqual(records.location("@perishlab/demo@1.2.3", {"integrity": INTEGRITY, "tarball": url}), url)

    def test_urls_refuse_without_normalization_or_input_disclosure(self):
        values = ["//foreign.invalid/x", "https://registry.npmjs.org@foreign.invalid/x", "https://registry.npmjs.org.evil.invalid/x", "https://registry.npmjs.org:444/demo/-/demo-1.2.3.tgz", "http://registry.npmjs.org/demo/-/demo-1.2.3.tgz", "https://registry.npmjs.org/demo/-/demo-1.2.3.tgz?SECRET=x", "https://registry.npmjs.org/demo/-/demo-1.2.3.tgz#SECRET", "https://registry.npmjs.org/demo/../demo/-/demo-1.2.3.tgz", "https://registry.npmjs.org/demo%2f-/demo-1.2.3.tgz", "https://registry.npmjs.org/demo\\-/demo-1.2.3.tgz", "https://registry.npmjs.org/demo/-/demo-2.0.0.tgz"]
        for url in values:
            with self.subTest(url=url), self.assertRaises(Refusal) as caught:
                records.location("demo@1.2.3", {"integrity": INTEGRITY, "tarball": url})
            self.assertNotIn("SECRET", str(caught.exception))

    def test_private_routing_requires_exact_scope_package_version(self):
        prefix = "https://npm.pkg.github.com/download/@perishlab/demo/1.2.3/"
        for url in [None, prefix + "a" * 39, prefix + "A" * 40, prefix + "a" * 40 + "?x=1", prefix.replace("demo", "other") + "a" * 40, prefix.replace("1.2.3", "2.0.0") + "a" * 40, prefix.replace("npm.pkg.github.com", "foreign.invalid") + "a" * 40]:
            with self.subTest(url=url), self.assertRaises(Refusal):
                records.location("@perishlab/demo@1.2.3", {"integrity": INTEGRITY, "tarball": url})

    def test_integrity_and_exotic_resolution_refuse(self):
        for value in ["sha1-AAAA", "sha512-AA==", INTEGRITY + " ", INTEGRITY[:-1], 1, None]:
            with self.subTest(value=value), self.assertRaises(Refusal):
                records.location("demo@1.2.3", {"integrity": value})
        for field in ["type", "directory", "repo", "gitHosted", "commit", "registry"]:
            with self.subTest(field=field), self.assertRaises(Refusal):
                records.location("demo@1.2.3", {"integrity": INTEGRITY, field: "SECRET"})

    def test_identity_and_snapshot_bounds(self):
        self.assertEqual(records.snapshot("demo@1.2.3(@types/node@24.10.1)(vite@8.2.1(@types/node@24.10.1))"), "demo@1.2.3")
        for key in ["demo@file:../x", "demo@https://foreign.invalid/x", "bad/name@1.2.3", "demo@01.2.3", "demo@1.2.3-01", "demo@1.2.3(peer@1.0.0", "demo@1.2.3(peer@file:../x)", "demo@1.2.3()", "demo@1.2.3(peer)", "demo@1.2.3(peer@1.0.0)junk", "demo@1.2.3" + "(peer@1.0.0" * 9 + ")" * 9]:
            with self.subTest(key=key), self.assertRaises(Refusal):
                records.snapshot(key)

    def test_requirement_ranges(self):
        cases = [("0", "0.99.0", True), ("0", "1.0.0", False), ("1.2.3", "1.2.4", False), ("^1.2.3", "1.9.0", True), ("^0.2.3", "0.3.0", False), ("^0.0.3", "0.0.4", False), ("~1.2.3", "1.3.0", False), ("1.2", "1.2.9", True), ("1", "2.0.0", False), ("1.2.3-rc.1", "1.2.3-rc.1", True), ("^1.2.3-rc.1", "1.2.3-rc.1", False)]
        for requirement, version, accepted in cases:
            with self.subTest(requirement=requirement, version=version):
                self.assertEqual(records.satisfies(requirement, version), accepted)
