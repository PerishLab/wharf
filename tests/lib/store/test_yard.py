import base64
import json
import tempfile
import unittest
from pathlib import Path

from lib.media import depot
from lib.refusal import Refusal
from lib.store import yard
from tests.lib.store.memory import Memory

HELD = {"repository": "PerishLab/concord", "marker": "v0.12.10", "kind": "changelog"}


def entry(path, body, executable=False):
    return {"path": path, "sha256": depot.sha(body), "executable": executable, "body": base64.b64encode(body).decode()}


def consign(bucket, objects, named=None, stored=None):
    named = named or HELD
    stored = stored or named
    body = json.dumps({"schema": yard.SCHEMA, **named, "objects": objects}).encode()
    digest = depot.sha(body)
    bucket.put(yard.key(stored["repository"], stored["marker"], stored["kind"], digest), body)
    return dict(stored, digest=digest)


class Yard(unittest.TestCase):
    def setUp(self):
        self.bucket = Memory()

    def test_a_consignment_reads_back_and_unpacks_its_objects(self):
        held = consign(self.bucket, [entry("en/INDEX.md", b"# notes\n"), entry("run.sh", b"#!/bin/sh\n", True)])
        target = yard.unpack(yard.read(self.bucket, held), tempfile.mkdtemp())
        self.assertEqual((Path(target) / "en/INDEX.md").read_bytes(), b"# notes\n")
        self.assertTrue((Path(target) / "run.sh").stat().st_mode & 0o111)

    def test_an_expired_consignment_asks_to_consign_again(self):
        with self.assertRaisesRegex(Refusal, "consign it again"):
            yard.read(self.bucket, dict(HELD, digest="0" * 64))

    def test_bytes_that_disagree_with_their_digest_refuse(self):
        held = consign(self.bucket, [entry("en/INDEX.md", b"notes")])
        self.bucket.put(yard.key(HELD["repository"], HELD["marker"], HELD["kind"], held["digest"]), b"{}")
        with self.assertRaisesRegex(Refusal, "does not hold the bytes its digest names"):
            yard.read(self.bucket, held)

    def test_a_consignment_for_another_marker_refuses(self):
        held = consign(self.bucket, [entry("en/INDEX.md", b"notes")], dict(HELD, marker="v0.12.9"), HELD)
        with self.assertRaisesRegex(Refusal, "names marker 'v0.12.9'"):
            yard.read(self.bucket, held)

    def test_objects_that_repeat_escape_or_drift_refuse(self):
        cases = {
            "names one path twice": [entry("a.md", b"a"), entry("a.md", b"b")],
            "not anchored": [entry("../a.md", b"a")],
            "does not hold the bytes it names": [dict(entry("a.md", b"a"), sha256=depot.sha(b"b"))],
        }
        for message, objects in cases.items():
            with self.subTest(message), self.assertRaisesRegex(Refusal, message):
                yard.unpack({"objects": objects}, tempfile.mkdtemp())

    def test_a_key_needs_a_digest_and_a_known_kind(self):
        with self.assertRaisesRegex(Refusal, "not a sha256"):
            yard.key("PerishLab/concord", "v0.12.10", "changelog", "abc")
        with self.assertRaisesRegex(Refusal, "not a depot kind"):
            yard.key("PerishLab/concord", "v0.12.10", "configuration", "0" * 64)
