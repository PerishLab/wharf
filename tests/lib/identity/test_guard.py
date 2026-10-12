import hashlib
import io
import json
import subprocess
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

from lib.identity import guard
from lib.identity.guard import coverage, pull, snapshot
from lib.media import release
from lib.refusal import Refusal
from tests.lib.store.memory import Memory

LINUX = "x86_64-unknown-linux-gnu"
COMMIT = "a" * 40
DEPOT = "0123456789abcdef" * 4
UNGUARDED = "0a39d831b34519176930506d747992a0d1f16fe2749db51a91f42edeaf9a9a9a"


def bound(name):
    root = Path(tempfile.mkdtemp())
    held = {}
    for target in release.LAYOUT["targets"]:
        directory = root / target
        directory.mkdir()
        suffix = ".exe" if "windows" in target else ""
        (directory / f"{name}-{target}{suffix}").write_bytes(f"binary {target}".encode())
        held[target] = directory
    return held


def managers():
    root = Path(tempfile.mkdtemp())
    (root / "manage.sh").write_text("sh")
    (root / "manage.ps1").write_text("ps1")
    return root


def answering(document, calls=None):
    def runner(argv, cwd, env=None):
        if calls is not None:
            calls.append((argv, env))
        return json.dumps(document) if isinstance(document, dict) else document
    return runner


def missing(argv, cwd, env=None):
    raise subprocess.CalledProcessError(2, argv, "", "error: unrecognized subcommand 'authority'")


def plumb(marker="v0.57.0"):
    return release.Release("PerishLab/plumb", marker, COMMIT, "b" * 40)


class Reported(unittest.TestCase):
    def test_reads_the_authority_the_bound_linux_binary_reports(self):
        calls = []
        held = guard.reported(plumb(), bound("plumb"), answering({"producer": f"v0.57.0@{COMMIT}", "depot": DEPOT}, calls))
        self.assertEqual(held, {"producer": f"v0.57.0@{COMMIT}", "depot": DEPOT})
        argv, env = calls[0]
        self.assertTrue(argv[0].endswith(f"/{LINUX}/plumb-{LINUX}"))
        self.assertEqual(argv[1:], ["release", "authority", "--json"])
        self.assertEqual(set(env), {"HOME", "PATH"})

    def test_a_prerelease_reports_its_own_marker(self):
        held = guard.reported(plumb("v0.57.0-rc.1"), bound("plumb"), answering({"producer": f"v0.57.0-rc.1@{COMMIT}", "depot": DEPOT}))
        self.assertEqual(held["producer"], f"v0.57.0-rc.1@{COMMIT}")

    def test_refuses_an_authority_that_does_not_match_the_release(self):
        cases = {
            "exactly": {"producer": f"v0.57.0@{COMMIT}", "depot": DEPOT, "extra": "x"},
            "as strings": {"producer": f"v0.57.0@{COMMIT}"},
            "producer version": {"producer": f"v0.56.9@{COMMIT}", "depot": DEPOT},
            "producer commit": {"producer": f"v0.57.0@{'c' * 40}", "depot": DEPOT},
            "producer commit ''": {"producer": "v0.57.0", "depot": DEPOT},
            "lowercase hex": {"producer": f"v0.57.0@{COMMIT}", "depot": DEPOT.upper()},
        }
        for reason, document in cases.items():
            with self.subTest(reason), self.assertRaisesRegex(Refusal, reason):
                guard.reported(plumb(), bound("plumb"), answering(document))
        with self.assertRaisesRegex(Refusal, "did not print JSON"):
            guard.reported(plumb(), bound("plumb"), answering("plumb v0.57.0"))

    def test_refuses_a_binary_that_lacks_the_command(self):
        with self.assertRaisesRegex(Refusal, "release authority --json exited 2; a guard-authority release needs a binary that reports it"):
            guard.reported(plumb(), bound("plumb"), missing)

    def test_refuses_a_release_without_its_linux_binary(self):
        held = bound("plumb")
        del held[LINUX]
        with self.assertRaisesRegex(Refusal, LINUX):
            guard.reported(plumb(), held, answering({}))

    def test_another_product_runs_nothing_and_reports_nothing(self):
        santi = release.Release("PerishLab/santi", "v1.2.3", COMMIT, "b" * 40)
        self.assertIsNone(guard.reported(santi, bound("santi"), missing))
        self.assertEqual(guard.GUARD["repositories"], ["PerishLab/plumb"])


class Sealed(unittest.TestCase):
    def publish(self, published, contents, bucket):
        with mock.patch.object(release, "generator", lambda held: {"version": "wharf fixed"}):
            return release.publish(published, contents, bucket, lambda url: bucket.get(url.split(".perish.uk/", 1)[1]))

    def contents(self, published, runner):
        name = published.repository.split("/", 1)[1]
        held = bound(name)
        return release.Contents(held, managers(), {target: [name] for target in held}, {}, lambda: guard.reported(published, held, runner))

    def test_another_products_seal_keeps_its_bytes(self):
        santi = release.Release("PerishLab/santi", "v1.2.3-rc.1", COMMIT, "b" * 40)
        bucket = Memory()
        self.publish(santi, self.contents(santi, missing), bucket)
        body = bucket.get(release.sealed(santi))
        self.assertNotIn(b'"guard"', body)
        self.assertEqual(hashlib.sha256(body).hexdigest(), UNGUARDED)

    def test_plumbs_seal_carries_the_guard_and_is_read_back(self):
        bucket = Memory()
        self.assertEqual(self.publish(plumb(), self.contents(plumb(), answering({"producer": f"v0.57.0@{COMMIT}", "depot": DEPOT})), bucket)["state"], "published")
        seal = json.loads(bucket.get(release.sealed(plumb())))
        self.assertEqual(seal["guard"], {"producer": f"v0.57.0@{COMMIT}", "depot": DEPOT})
        self.assertEqual(seal["releaseVersion"], seal["guard"]["producer"].split("@")[0])
        self.assertEqual(seal["commit"], seal["guard"]["producer"].split("@")[1])

    def test_a_refused_guard_writes_nothing(self):
        bucket = Memory()
        with self.assertRaisesRegex(Refusal, "release authority --json exited 2"):
            self.publish(plumb(), self.contents(plumb(), missing), bucket)
        self.assertEqual(bucket.writes, [])

    def test_rerunning_a_published_release_does_not_ask_the_binary_again(self):
        bucket = Memory()
        contents = self.contents(plumb(), answering({"producer": f"v0.57.0@{COMMIT}", "depot": DEPOT}))
        self.publish(plumb(), contents, bucket)
        written = bucket.get(release.sealed(plumb()))
        rerun = release.Contents(contents.bound, contents.managers, contents.installed, {}, lambda: guard.reported(plumb(), contents.bound, missing))
        self.assertEqual(self.publish(plumb(), rerun, bucket)["state"], "already-published")
        self.assertEqual(bucket.get(release.sealed(plumb())), written)

    def test_a_seal_that_is_not_served_as_written_refuses(self):
        contents = self.contents(plumb(), answering({"producer": f"v0.57.0@{COMMIT}", "depot": DEPOT}))
        with self.assertRaisesRegex(Refusal, "does not serve the written seal"):
            release.publish(plumb(), contents, Memory(), lambda url: b"{}")

if __name__ == "__main__":
    unittest.main()

COMMAND = ["cargo", "test", "--locked"]


def proved(ok=True, strength="full", names=("guard/rust", "guard/test")):
    actions = [{"name": name, "input": "a" * 64, "world": "b" * 64} for name in names]
    return {"guard": {"schema": "plumb.guard-runtime/v1", "ok": ok, "strength": strength, "guard": {"schema": "plumb.guard-proof/v1", "actions": actions}}}


class Tested(unittest.TestCase):
    def test_a_successful_full_guard_that_ran_the_test_action_covers_the_suite(self):
        self.assertTrue(coverage.tested(proved()))

    def test_anything_less_leaves_the_suite_planned(self):
        for held in (proved(names=("guard/rust",)), proved(strength="fast"), proved(ok=False), {"guard": {"ok": True, "strength": "full"}}, {}, None):
            with self.subTest(held=held):
                self.assertFalse(coverage.tested(held))

    def test_the_skip_relies_on_the_command_plumb_pins_for_the_test_action(self):
        message = "suite linux is skipped because guard/test runs this on a virtual workspace, pinned in Plumb too (PerishLab/plumb#229); change both together"
        self.assertEqual((coverage.SUITE["action"], coverage.SUITE["command"]), ("guard/test", COMMAND), message)


HEAD = "d" * 40
MARKER = "b" * 40
LIFTED = "c" * 40
AUTHORITY = {"producer": "v0.87.0@" + "2" * 40, "depot": "3" * 64}


def runtime(commit="9" * 40, tree=MARKER, **changed):
    proof = {"schema": "plumb.guard-proof/v1", "repository": "PerishLab/example", "tree": tree, "plumb": AUTHORITY["producer"], "depot": AUTHORITY["depot"], "platform": "linux", "digest": "4" * 64, "actions": [{"name": "guard/test", "input": "5" * 64, "world": "6" * 64}], "resolution": {"context": "ci-latest", "tree": LIFTED, "packages": []}}
    proof.update(changed.pop("proof", {}))
    return json.dumps(dict({"schema": "plumb.guard-runtime/v1", "root": "/runner/work/example", "ok": True, "strength": "full", "boundary": "head", "commit": commit, "digest": "1" * 64, "guard": proof}, **changed))


def checked(body):
    held = {"request": snapshot.Request(Path("/source"), {"repository": "PerishLab/example", "marker": "v1.0.0", "commit": "a" * 40, "tree": MARKER}, {}), "tools": {"plumb": {"version": "plumb v0.87.0"}}, "authority": AUTHORITY}
    return snapshot.proof(held, HEAD, {"tree": LIFTED, "packages": []}, body)


class Adopted(unittest.TestCase):
    def test_the_plan_guard_and_a_pull_request_guard_of_the_marker_tree_both_prove_the_combination(self):
        self.assertEqual(checked(runtime(commit=HEAD, tree=LIFTED))["commit"], HEAD)
        self.assertEqual(checked(runtime())["commit"], "9" * 40)

    def test_a_pull_request_guard_of_anything_else_refuses(self):
        resolution = {"context": "ci-latest", "tree": LIFTED, "packages": [{"ecosystem": "cargo", "name": "plumb", "version": "0.1.0"}]}
        for body in (runtime(tree=LIFTED), runtime(proof={"resolution": resolution}), runtime(proof={"depot": "7" * 64}), runtime(ok=False), runtime(strength="fast"), runtime(commit=None), runtime(commit=HEAD)):
            with self.subTest(body=body), self.assertRaises(Refusal):
                checked(body)

    def test_the_first_admitted_offer_is_adopted_and_none_without_one(self):
        held = {"request": snapshot.Request(Path("/source"), {"repository": "PerishLab/example", "tree": MARKER}, {})}
        with mock.patch.object(snapshot, "proof", side_effect=[Refusal("other"), {"proved": "pull"}]):
            self.assertEqual(snapshot.adopted(held, HEAD, {}, ["other", "pull", "unread"]), {"proved": "pull"})
        with mock.patch.object(snapshot, "proof", side_effect=Refusal("other")):
            self.assertIsNone(snapshot.adopted(held, HEAD, {}, ["other"]))


def zipped(members):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as held:
        for name, body in members.items():
            held.writestr(name, body)
    return buffer.getvalue()


class Client:
    def __init__(self, replies):
        self.replies = replies
        self.seen = []

    def open(self, request, timeout):
        self.seen.append((request.full_url, request.get_header("Authorization")))
        reply = self.replies[request.full_url]
        if isinstance(reply, Exception):
            raise reply
        return io.BytesIO(reply)


API = "https://api.github.com/repos/PerishLab/example"
BLOB = "https://blob.example/guard?signature=s"


def offering(artifacts, trees, download):
    replies = {f"{API}/actions/artifacts?name=guard-runtime&per_page=50": json.dumps({"artifacts": artifacts}).encode()}
    replies.update({f"{API}/git/commits/{head}": json.dumps({"tree": {"sha": tree}}).encode() for head, tree in trees.items()})
    replies.update({f"{API}/actions/artifacts/{artifact['id']}/zip": download for artifact in artifacts})
    replies[BLOB] = zipped({"guard-runtime.json": runtime()})
    actions = pull.Actions("reader", "PerishLab/example")
    actions._client = Client(replies)
    return actions


def artifact(identity, head, **changed):
    return dict({"id": identity, "name": "guard-runtime", "expired": False, "workflow_run": {"id": identity * 10, "head_sha": head}}, **changed)


class Offered(unittest.TestCase):
    def test_only_unexpired_verdicts_of_the_marker_tree_are_downloaded_without_the_token(self):
        moved = urllib.error.HTTPError("", 302, "Found", {"Location": BLOB}, None)
        actions = offering([artifact(1, "e" * 40), artifact(2, "f" * 40, expired=True), artifact(3, "a" * 40)], {"e" * 40: "0" * 40, "a" * 40: MARKER}, moved)
        found = list(pull.listed(actions, MARKER))
        self.assertEqual(found, [({"run": 30, "artifact": 3, "head": "a" * 40}, "1" * 64, runtime())])
        self.assertEqual(actions._client.seen[-1], (BLOB, None))
        self.assertTrue(all(authorization == "Bearer reader" for url, authorization in actions._client.seen[:-1]))

    def test_a_download_that_does_not_redirect_or_holds_more_than_the_document_refuses(self):
        for download, blob in ((b"zip", None), (urllib.error.HTTPError("", 302, "Found", {"Location": BLOB}, None), {"guard-runtime.json": "{}", "other": "x"})):
            actions = offering([artifact(3, "a" * 40)], {"a" * 40: MARKER}, download)
            if blob:
                actions._client.replies[BLOB] = zipped(blob)
            with self.subTest(download=download), self.assertRaises(Refusal):
                list(pull.listed(actions, MARKER))

    def test_no_token_or_an_unreadable_listing_refuses_with_its_reason(self):
        with self.assertRaisesRegex(Refusal, "no pull request Guard reader token"):
            pull.offered("PerishLab/example", MARKER, {})
        with mock.patch.object(pull, "listed", side_effect=urllib.error.URLError("down")), self.assertRaisesRegex(Refusal, "unreadable: URLError"):
            pull.offered("PerishLab/example", MARKER, {"WHARF_GUARD_TOKEN": "reader"})
