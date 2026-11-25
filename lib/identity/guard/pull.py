import io
import json
import os
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from lib import process
from lib.content import resources
from lib.content.lane import preview
from lib.refusal import Refusal

PULL = resources.read_json("identity/pull.json")
FAILURES = (OSError, ValueError, KeyError, TypeError, AttributeError, zipfile.BadZipFile, subprocess.SubprocessError)


class Held(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *arguments):
        return None


class Actions:
    def __init__(self, token, repository, invoke=process.github):
        if not isinstance(token, str) or not token or any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise Refusal("pull request Guard reader requires a prepared read-only token")
        preview.matches(repository, preview.REPOSITORY, "pull request Guard repository")
        self._owner, self._name = repository.split("/", 1)
        self._token = token
        self._invoke = invoke

    def artifacts(self):
        return self._invoke(
            ["artifacts", self._owner, self._name, "--name", PULL["artifact"], "--size", str(PULL["candidates"])],
            timeout=30,
            limit=PULL["limit"],
            token=self._token,
        )["artifacts"]

    def tree(self, commit):
        preview.matches(commit, preview.HEX[40], "pull request Guard head")
        return self._invoke(
            ["commit", self._owner, self._name, commit],
            timeout=30,
            limit=PULL["limit"],
            token=self._token,
        )["tree"]["sha"]

    def archive(self, artifact):
        identity = int(artifact["id"])
        size = int(artifact["size_in_bytes"])
        digest = artifact.get("digest")
        if not 0 < size <= PULL["limit"] or not isinstance(digest, str) or not digest.startswith("sha256:"):
            raise Refusal("pull request Guard artifact lacks bounded size or digest")
        with tempfile.TemporaryDirectory(prefix="wharf-guard-artifact-") as directory:
            target = Path(directory) / "artifact.zip"
            self._invoke(
                ["download", self._owner, self._name, str(identity), "--size", str(size), "--digest", digest, "--output", str(target)],
                timeout=30,
                token=self._token,
            )
            data = target.read_bytes()
        if len(data) > PULL["limit"]:
            raise Refusal("pull request Guard reply exceeds its budget")
        return data


def unpacked(archive):
    with zipfile.ZipFile(io.BytesIO(archive)) as held:
        if held.namelist() != [PULL["member"]] or held.getinfo(PULL["member"]).file_size > PULL["limit"]:
            raise Refusal("pull request Guard artifact must hold exactly its runtime document")
        return held.read(PULL["member"]).decode()


def listed(actions, tree):
    for artifact in actions.artifacts()[:PULL["candidates"]]:
        run = artifact.get("workflow_run") or {}
        if artifact.get("expired") or artifact.get("name") != PULL["artifact"] or actions.tree(run.get("head_sha")) != tree:
            continue
        body = unpacked(actions.archive(artifact))
        yield {"run": run["id"], "artifact": artifact["id"], "head": run["head_sha"]}, json.loads(body)["digest"], body


def offered(repository, tree, environ=os.environ):
    token = environ.get(PULL["token"])
    if not token:
        raise Refusal("no pull request Guard reader token was prepared")
    try:
        return list(listed(Actions(token, repository), tree))
    except FAILURES as error:
        raise Refusal(f"pull request Guard verdicts are unreadable: {type(error).__name__}") from error
