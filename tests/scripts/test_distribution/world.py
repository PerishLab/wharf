import contextlib
from pathlib import Path
from unittest import mock

from lib.content import canonical
from lib.media import release
from lib.process.trial import admission, context, credential, preparation, transport
from lib.refusal import Refusal
from tests.lib.store.memory import Memory
from tests.scripts.test_distribution import publisher, report

OBSERVER = {"run": "20", "attempt": "1", "wharf": "a" * 40}
STARTED = "2026-10-29T15:00:00Z"
AUTHORITY = "https://releases.plumb.perish.uk"


class Bucket(Memory):
    def snapshot(self, key):
        return (self.get(key), self.head(key)) if self.exists(key) else (None, None)


class World:
    def __init__(self):
        self.blob, self.public, self.documents = Bucket(), Bucket(), {}
        self.source = {"repository": "PerishLab/plumb", "marker": "v1.2.3-rc.1", "commit": "b" * 40, "tree": "c" * 40}
        scenes = {"authority": "https://depot.seatrial.perish.uk", "product": "seatrial", "channel": "stable"}
        self.specification = {"cases": ["writer"], "scenes": scenes, "image": "ghcr.io/perishlab/plumb"}
        trial = {"schema": "wharf.trial.declaration/v1", "source": self.source, "manifest": "d" * 64, "authority": AUTHORITY, "specification": self.specification}
        self.distribution = dict(self.source, completed={"run": "7", "attempt": "1", "wharf": "a" * 40}, state="complete", media={"binaries": "published"}, trial=trial)
        self.documents[f"{AUTHORITY}/v1/releases/rc/{self.source['marker']}/distribution.json"] = canonical.encode(self.distribution)
        self.product = self.channel("plumb", "rc", (self.source["marker"], self.source["commit"]))
        self.runner = self.channel("seatrial", "stable", ("v1.0.0", "e" * 40))
        manifest = self.entry(scenes["authority"], "objects/manifest.json", {"kind": "scene", "files": []})
        self.scene = {"format": 3, "product": "seatrial", "channel": "stable", "kind": "scene", "generation": "f" * 64, "manifest": manifest}
        self.documents[f"{scenes['authority']}/channels/stable/scenes/latest.json"] = canonical.encode(self.scene)
        self.admitted = admission.publication(publisher(), self.source, self.distribution)
        with self.wired():
            self.prepared = preparation.freeze(self.admitted, OBSERVER, "declared", STARTED)

    def entry(self, authority, key, value):
        body = canonical.encode(value)
        entry = release.remote(authority, key, body, release.JSON)
        self.documents[entry["url"]] = body
        return entry

    def channel(self, name, channel, source):
        version, commit = source
        authority = f"https://releases.{name}.perish.uk"
        seal = {"product": name, "channel": channel, "releaseVersion": version, "commit": commit, "artifacts": {"linux-x64": {"sha256": "b" * 64}}}
        pointer = dict(seal, seal=self.entry(authority, f"v1/releases/{channel}/{version}/seal.json", seal))
        self.documents[f"{authority}/v1/channels/{channel}.json"] = canonical.encode(pointer)
        return {"pointer": pointer, "seal": seal}

    def fetch(self, url):
        prefix = AUTHORITY + "/"
        if url.startswith(prefix) and url[len(prefix):] in self.public.objects:
            return self.public.get(url[len(prefix):])
        if url not in self.documents:
            raise Refusal("unexpected public read")
        return self.documents[url]

    @contextlib.contextmanager
    def wired(self):
        with contextlib.ExitStack() as stack:
            for target, name, value in ((release, "fetch", self.fetch), (admission, "fetch", self.fetch), (transport.r2, "configured", lambda: self.blob), (transport.r2, "writer", lambda name, role: self.public), (context, "api", lambda path: publisher())):
                stack.enter_context(mock.patch.object(target, name, side_effect=value))
            stack.enter_context(mock.patch.object(transport.parameters, "answer"))
            stack.enter_context(mock.patch.object(preparation.oci, "public_digest", return_value="sha256:" + "b" * 64))
            yield

    def execution(self):
        return {"state": "executed", "exit": 0, "cases": ["writer"], "scope": "declared", "scenes": self.specification["scenes"], "product_pointer": self.product["pointer"]}

    def report(self):
        product = {"name": "plumb", "channel": "rc", "version": self.source["marker"], "commit": self.source["commit"], "seal": self.product["pointer"]["seal"]["sha256"], "archive": "b" * 64}
        return dict(report(), products=[product], scene=self.scene, groups=[{"digest": "c" * 64, "cases": ["writer"]}], images=[{"name": "plumb", "reference": "ghcr.io/perishlab/plumb:1.2.3-rc.1", "digest": "ghcr.io/perishlab/plumb@sha256:" + "b" * 64}])

    def installed(self):
        return dict(self.runner, sha256="a" * 64, version="seatrial v1.0.0")

    def write(self, directory):
        evidence = Path(directory)
        evidence.mkdir(parents=True, exist_ok=True)
        values = {"prepared": self.prepared, "admission": self.admitted, "execution": self.execution(), "report": self.report(), "runner": self.installed(), "observation": {"ok": True, "state": "passed"}}
        for name, value in values.items():
            (evidence / f"{name}.json").write_bytes(canonical.encode(value))
        (evidence / "run.log").write_text("original assertion output\n")
        return evidence


def producer():
    return {"status": "completed", "conclusion": "success", "steps": [{"name": name, "status": "completed", "conclusion": "success"} for name in credential.STEPS]}
