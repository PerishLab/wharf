import urllib.error
import urllib.request

from lib.content import resources
from lib.content.static import evidence
from lib.process.cfworker import mapping, observation
from lib.refusal import Refusal

class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *arguments):
        raise Refusal("static provider redirects are not admitted")


class Reader:
    def __init__(self, token, registered):
        if not isinstance(token, str) or not token or any(not 33 <= ord(char) <= 126 for char in token):
            raise Refusal("static provider needs a prepared read-only token")
        self._token = token
        self._registered = mapping.selected(registered)
        self._client = urllib.request.build_opener(urllib.request.ProxyHandler({}), Redirect())

    def get(self, suffix):
        name = self._registered["name"]
        allowed = ("", f"/previews/{name}")
        deployment = f"/previews/{name}/deployments/"
        if suffix not in allowed and (not isinstance(suffix, str) or not suffix.startswith(deployment) or not observation.DEPLOYMENT.fullmatch(suffix[len(deployment):])):
            raise Refusal("static provider read needs one exact fixed lane identity endpoint")
        policy = resources.read_json("build.json")["cfworker"]
        account = self._registered["account"]
        parent = self._registered["parent"]
        url = f"{policy['api']}/accounts/{account}/workers/workers/{parent}{suffix}"
        headers = {"Authorization": "Bearer " + self._token, "Accept": "application/json", "Accept-Encoding": "identity", "User-Agent": "wharf-lane"}
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with self._client.open(request, timeout=30) as response:
                if response.status != 200 or response.geturl() != url or response.headers.get("Content-Encoding") not in (None, "identity"):
                    raise Refusal("static provider read did not return its exact untransformed endpoint")
                body = response.read(evidence.LIMIT + 1)
        except (OSError, urllib.error.URLError, TimeoutError) as error:
            raise Refusal("static provider read failed; no absence or authority inferred") from error
        reply = evidence.decode(body)
        if not isinstance(reply, dict) or reply.get("success") is not True or reply.get("errors") != [] or not isinstance(reply.get("result"), dict):
            raise Refusal("static provider read is not a successful object observation")
        return reply["result"]

    def parent(self):
        return self.get("")

    def observe(self, policy, identity):
        if not isinstance(identity, str) or not observation.DEPLOYMENT.fullmatch(identity):
            raise Refusal("cfworker observation needs an exact deployment identity")
        policy = observation.prepared(policy)
        held = observation.parent(self.parent(), self._registered, policy)
        selected = observation.preview(self.preview(), self._registered, held)
        deployed = observation.deployment(self.deployment(identity), selected, identity)
        if observation.parent(self.parent(), self._registered, policy) != held:
            raise Refusal("cfworker parent changed during observation")
        return {"parent": held, "preview": selected, "deployment": deployed}

    def preview(self):
        return self.get(f"/previews/{self._registered['name']}")

    def deployment(self, identity):
        if not isinstance(identity, str) or not observation.DEPLOYMENT.fullmatch(identity):
            raise Refusal("static provider deployment requires an exact UUID, not latest")
        return self.get(f"/previews/{self._registered['name']}/deployments/{identity}")
