import urllib.error
import urllib.request

from lib.content import resources
from lib.content.static import evidence
from lib.process.cfworker import inventory, mapping, observation
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
        reply = self.reply(suffix)
        if not isinstance(reply.get("result"), dict):
            raise Refusal("static provider read is not a successful object observation")
        return reply["result"]

    def reply(self, suffix):
        name = self._registered["name"]
        listing = tuple(f"/previews?page={number}&per_page={inventory.SIZE}" for number in range(1, inventory.PAGES + 1))
        history = tuple(f"/previews/{name}/deployments?page={number}&per_page={inventory.SIZE}" for number in range(1, inventory.PAGES + 1))
        allowed = ("", f"/previews/{name}", *listing, *history)
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
        if not isinstance(reply, dict) or reply.get("success") is not True or "errors" not in reply or reply["errors"] not in (None, []):
            raise Refusal("static provider read is not a successful observation")
        return reply

    def page(self, number):
        if type(number) is not int or not 1 <= number <= inventory.PAGES:
            raise Refusal("cfworker inventory needs one bounded exact page number")
        return self.reply(f"/previews?page={number}&per_page={inventory.SIZE}")

    def parent(self):
        return self.get("")

    def deployment_page(self, number):
        if type(number) is not int or not 1 <= number <= inventory.PAGES:
            raise Refusal("cfworker deployment catalog needs one bounded exact page number")
        return self.reply(f"/previews/{self._registered['name']}/deployments?page={number}&per_page={inventory.SIZE}")

    def status(self, policy):
        policy = observation.prepared(policy)
        held = observation.parent(self.parent(), self._registered, policy)
        catalog = inventory.read(self, held)
        selected = next((item for item in catalog if item["name"] == self._registered["name"]), None)
        if selected is not None and observation.resource(self.preview(), self._registered, held) != selected:
            raise Refusal("cfworker selected Preview moved after inventory")
        if inventory.read(self, held) != catalog:
            raise Refusal("cfworker Preview inventory moved during status")
        if observation.parent(self.parent(), self._registered, policy) != held:
            raise Refusal("cfworker parent changed during status")
        return {"parent": held, "previews": catalog, "preview": selected}

    def history(self, policy):
        before = self.status(policy)
        selected = before["preview"]
        if selected is None:
            raise Refusal("cfworker deployment catalog needs a present selected Preview")
        catalog = inventory.deployments(self, selected)
        if inventory.deployments(self, selected) != catalog:
            raise Refusal("cfworker retained deployment catalog moved during observation")
        if self.status(policy) != before:
            raise Refusal("cfworker parent, selected Preview or siblings moved during deployment observation")
        return dict(before, deployments=catalog)

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
