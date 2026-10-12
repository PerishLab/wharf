import io
import json
import os
import urllib.error
import urllib.request
import zipfile

from lib.content import resources
from lib.content.lane import preview
from lib.refusal import Refusal

PULL = resources.read_json("identity/pull.json")
FAILURES = (OSError, ValueError, KeyError, TypeError, AttributeError, zipfile.BadZipFile)


class Held(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *arguments):
        return None


class Actions:
    def __init__(self, token, repository):
        if not isinstance(token, str) or not token or any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise Refusal("pull request Guard reader requires a prepared read-only token")
        preview.matches(repository, preview.REPOSITORY, "pull request Guard repository")
        self._token = token
        self._repository = repository
        self._client = urllib.request.build_opener(urllib.request.ProxyHandler({}), Held())

    def read(self, url, authorized):
        headers = {"User-Agent": "wharf-plan", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": PULL["version"]}
        if authorized:
            headers["Authorization"] = "Bearer " + self._token
        with self._client.open(urllib.request.Request(url, headers=headers, method="GET"), timeout=30) as response:
            body = response.read(PULL["limit"] + 1)
        if len(body) > PULL["limit"]:
            raise Refusal("pull request Guard reply exceeds its budget")
        return body

    def get(self, path):
        return json.loads(self.read(f"{PULL['api']}/repos/{self._repository}{path}", True))

    def tree(self, commit):
        preview.matches(commit, preview.HEX[40], "pull request Guard head")
        return self.get(f"/git/commits/{commit}")["tree"]["sha"]

    def archive(self, identity):
        try:
            self.read(f"{PULL['api']}/repos/{self._repository}/actions/artifacts/{int(identity)}/zip", True)
        except urllib.error.HTTPError as error:
            location = error.headers.get("Location", "") if error.code == 302 else ""
        else:
            location = ""
        if not location.startswith("https://"):
            raise Refusal("pull request Guard artifact did not redirect to its download")
        return self.read(location, False)


def unpacked(archive):
    with zipfile.ZipFile(io.BytesIO(archive)) as held:
        if held.namelist() != [PULL["member"]] or held.getinfo(PULL["member"]).file_size > PULL["limit"]:
            raise Refusal("pull request Guard artifact must hold exactly its runtime document")
        return held.read(PULL["member"]).decode()


def listed(actions, tree):
    reply = actions.get(f"/actions/artifacts?name={PULL['artifact']}&per_page={PULL['candidates']}")
    for artifact in reply["artifacts"][:PULL["candidates"]]:
        run = artifact.get("workflow_run") or {}
        if artifact.get("expired") or artifact.get("name") != PULL["artifact"] or actions.tree(run.get("head_sha")) != tree:
            continue
        body = unpacked(actions.archive(artifact["id"]))
        yield {"run": run["id"], "artifact": artifact["id"], "head": run["head_sha"]}, json.loads(body)["digest"], body


def offered(repository, tree, environ=os.environ):
    token = environ.get(PULL["token"])
    if not token:
        raise Refusal("no pull request Guard reader token was prepared")
    try:
        return list(listed(Actions(token, repository), tree))
    except FAILURES as error:
        raise Refusal(f"pull request Guard verdicts are unreadable: {type(error).__name__}") from error
