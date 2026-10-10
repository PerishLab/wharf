import re
import urllib.error
import urllib.parse
import urllib.request

from lib.content import resources
from lib.content.lane import preview
from lib.content.static import evidence
from lib.refusal import Refusal

LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}(?:\[bot\])?")
LIMIT = 1048576


class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *arguments):
        raise Refusal("Preview GitHub redirects are not admitted")


class GitHub:
    def __init__(self, token):
        if not isinstance(token, str) or not token or any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise Refusal("Preview GitHub requires a prepared read-only token")
        self._token = token
        self._client = urllib.request.build_opener(urllib.request.ProxyHandler({}), Redirect())

    def get(self, path):
        repository = r"[A-Za-z0-9_-]+/[A-Za-z0-9_-]+"
        suffix = r"(?:/actions/runs/[1-9][0-9]*|/git/commits/[0-9a-f]{40}|/collaborators/[A-Za-z0-9][A-Za-z0-9-]{0,38}(?:%5Bbot%5D)?/permission)?"
        if not isinstance(path, str) or not re.fullmatch(r"/repos/" + repository + suffix, path):
            raise Refusal("Preview GitHub endpoint is not a fixed read-only identity endpoint")
        policy = resources.read_json("build.json")["admission"]
        headers = {"Authorization": "Bearer " + self._token, "Accept": "application/vnd.github+json", "User-Agent": "wharf-preview", "X-GitHub-Api-Version": policy["version"]}
        request = urllib.request.Request(policy["api"] + path, headers=headers, method="GET")
        try:
            with self._client.open(request, timeout=30) as response:
                if response.status != 200 or response.geturl() != request.full_url:
                    raise Refusal("Preview GitHub identity read did not return its exact endpoint")
                body = response.read(LIMIT + 1)
        except (OSError, urllib.error.URLError, TimeoutError) as error:
            raise Refusal("Preview GitHub identity read failed; no authorization inferred") from error
        value = evidence.decode(body)
        if not isinstance(value, dict):
            raise Refusal("Preview GitHub identity reply must be an object")
        return value

    def repository(self, name):
        preview.matches(name, preview.REPOSITORY, "GitHub repository")
        return self.get("/repos/" + name)

    def run(self, identity):
        if type(identity) is not int or identity < 1:
            raise Refusal("Preview GitHub run identity must be positive")
        return self.get(f"/repos/{resources.read_json('build.json')['admission']['repository']}/actions/runs/{identity}")

    def permission(self, repository, login):
        preview.matches(repository, preview.REPOSITORY, "GitHub permission repository")
        preview.matches(login, LOGIN, "GitHub actor login")
        return self.get(f"/repos/{repository}/collaborators/{urllib.parse.quote(login, safe='')}/permission")

    def commit(self, repository, identity):
        preview.matches(repository, preview.REPOSITORY, "GitHub source repository")
        preview.matches(identity, preview.HEX[40], "GitHub source commit")
        return self.get(f"/repos/{repository}/git/commits/{identity}")


class Routing:
    def __init__(self, repository, control, product):
        preview.matches(repository, preview.REPOSITORY, "scoped product repository")
        self._repository = repository
        self._control = control
        self._product = product

    def selected(self, repository):
        if repository != self._repository:
            raise Refusal("Preview product identity read is outside its scoped repository")

    def repository(self, name):
        if name == resources.read_json("build.json")["admission"]["repository"]:
            return self._control.repository(name)
        self.selected(name)
        return self._product.repository(name)

    def run(self, identity):
        return self._control.run(identity)

    def permission(self, repository, login):
        self.selected(repository)
        return self._product.permission(repository, login)

    def commit(self, repository, identity):
        self.selected(repository)
        return self._product.commit(repository, identity)
