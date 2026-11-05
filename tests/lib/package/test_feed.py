import hashlib
import unittest
from unittest import mock

from lib.package.openwrt import feed
from lib.refusal import Refusal
from tests.lib.store.memory import Memory

AUTHORITY = "https://releases.dynet.perish.uk"
INDEX = b"signed index"
PUBLIC = b"public key"
PACKAGE = b"signed package"


def put(bucket, body, name):
    digest = hashlib.sha256(body).hexdigest()
    key = f"v1/objects/sha256/{digest}/{name}"
    bucket.create(key, body)
    return {"url": f"{AUTHORITY}/{key}", "sha256": digest, "size": len(body)}


class Feed(unittest.TestCase):
    def setUp(self):
        self.bucket = Memory()
        self.seal = {"releaseVersion": "v1.2.3-rc.1", "artifacts": {feed.LAYOUT[kind]["key"]: put(self.bucket, body, name) for kind, body, name in (("apk", PACKAGE, "dynet-1.2.3_rc1-r0.apk"), ("apk-index", INDEX, "packages.adb"), ("apk-key", PUBLIC, "signing.pem"))}}
        self.key = "v1/apk/channels/rc/aarch64_cortex-a53/packages.adb"

    def read(self, url):
        return self.bucket.get(url.removeprefix(AUTHORITY + "/"))

    def point(self, version="1.2.3_rc1-r0", order="<"):
        with mock.patch.object(feed, "indexed", return_value=version), mock.patch.object(feed, "run", return_value=order):
            return feed.point(self.bucket, AUTHORITY, self.seal, self.read)

    def test_a_release_without_apk_has_no_feed_effect(self):
        self.assertIsNone(feed.point(self.bucket, AUTHORITY, {"artifacts": {}}, self.read))
        self.assertFalse(self.bucket.exists(self.key))
        self.assertIsNone(feed.point(self.bucket, AUTHORITY, {"managers": {}}, self.read))

    def test_activation_reads_public_bytes_and_swaps_the_exact_signed_index(self):
        self.assertEqual(self.point()["state"], "advanced")
        self.assertEqual(self.bucket.get(self.key), INDEX)

    def test_a_later_version_is_kept_and_an_equal_version_cannot_change_bytes(self):
        self.bucket.create(self.key, b"newer index")
        with self.assertRaisesRegex(Refusal, "newer OpenWrt feed"):
            self.point(order=">")
        with self.assertRaisesRegex(Refusal, "different bytes"):
            self.point(order="=")
        self.assertEqual(self.bucket.get(self.key), b"newer index")

    def test_missing_or_different_public_bytes_prevent_activation(self):
        with mock.patch.object(feed, "indexed", return_value="1.2.3_rc1-r0"), self.assertRaisesRegex(Refusal, "publicly"):
            feed.point(self.bucket, AUTHORITY, self.seal, lambda url: b"tampered")
        self.assertFalse(self.bucket.exists(self.key))

    def test_a_foreign_object_authority_is_not_read(self):
        held = dict(feed.entry(self.seal, "apk-index"), url="https://foreign.invalid/index")
        with self.assertRaisesRegex(Refusal, "outside"):
            feed.content(self.bucket, AUTHORITY, held)

    def test_index_signature_failure_prevents_activation(self):
        with mock.patch.object(feed, "indexed", side_effect=Refusal("signature")), self.assertRaisesRegex(Refusal, "signature"):
            feed.point(self.bucket, AUTHORITY, self.seal, self.read)
        self.assertFalse(self.bucket.exists(self.key))

    def test_index_reader_requires_one_package_and_a_verified_signature(self):
        for text in ("", "    version: 1-r0\n    version: 2-r0\n"):
            def runner(argv, cwd):
                return text if "adbdump" in argv else ""
            with self.subTest(text=text), self.assertRaisesRegex(Refusal, "exactly one"):
                feed.indexed(INDEX, PUBLIC, runner)

    def test_an_equal_retry_still_requires_public_feed_readback(self):
        self.bucket.create(self.key, INDEX)
        def reader(url):
            return b"stale" if url.endswith(self.key) else self.read(url)
        with mock.patch.object(feed, "indexed", return_value="1.2.3_rc1-r0"), mock.patch.object(feed, "run", return_value="="), self.assertRaisesRegex(Refusal, "does not read back"):
            feed.point(self.bucket, AUTHORITY, self.seal, reader)

    def test_wrong_release_version_never_reaches_publication(self):
        with self.assertRaisesRegex(Refusal, "differs from its release marker"):
            self.point(version="9.0.0-r0")
        self.assertFalse(self.bucket.exists(self.key))
