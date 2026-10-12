import threading
import urllib.error
import urllib.request

from lib.content import canonical, resources
from lib.content.lane import codec
from lib.content.static import evidence
from lib.process.cfworker import apply, discard, mapping, observation, reader
from lib.refusal import Refusal
from lib.store.lane import documents

CEILING = 99


def prepare(plan, observed, policy):
    return checked({"apply": plan, "before": observed, "policy": policy})


def checked(value):
    value = documents.object_value(documents.decode(documents.encode(value)), ("apply", "before", "policy"))
    plan = apply.checked(value["apply"])
    policy = observation.prepared(value["policy"])
    before = discard.status(value["before"], mapping.selected(plan["registration"]), policy)
    if before["preview"] is not None or plan["current"]["observed"] != {"state": "absent"}:
        raise Refusal("cfworker create needs exact reserved and observed selected absence")
    if len(before["previews"]) >= CEILING:
        raise Refusal("cfworker create requires headroom below the minimum native Preview quota; no eviction")
    return {"apply": plan, "before": before, "policy": policy}


def identified(value, plan):
    selected = mapping.selected(plan["apply"]["registration"])
    if not isinstance(value, dict) or value.get("name") != selected["name"] or value.get("slug") != selected["name"]:
        raise Refusal("cfworker create reply differs from its exact Preview name and slug")
    identity = observation.identity(value.get("id"))
    before = plan["before"]
    if identity in {item["id"] for item in before["previews"]} or identity in {item["name"] for item in before["previews"]} | {selected["name"]}:
        raise Refusal("cfworker created identity collides with a sibling or mutable Preview name")
    return {"id": identity, "name": selected["name"], "parent": before["parent"]["id"]}


def confirmed(plan, observed, created):
    plan = checked(plan)
    created = documents.object_value(created, ("id", "name", "parent"))
    identity = identified(dict(created, slug=created["name"]), plan)
    if created != identity:
        raise Refusal("cfworker create readback has a foreign parent identity")
    selected = mapping.selected(plan["apply"]["registration"])
    after = discard.status(observed, selected, plan["policy"])
    expected = sorted(plan["before"]["previews"] + [identity], key=lambda item: item["name"])
    if after["parent"] != plan["before"]["parent"] or after["preview"] != identity or after["previews"] != expected:
        raise Refusal("cfworker create readback changed parent, selected identity or siblings")
    return identity


class Creator:
    def __init__(self, token, plan):
        if not isinstance(token, str) or not token or any(not 33 <= ord(char) <= 126 for char in token):
            raise Refusal("cfworker create needs a separately prepared creation credential")
        self._plan = checked(plan)
        self._selected = mapping.selected(self._plan["apply"]["registration"])
        self._request = codec.digest("request", self._plan["apply"]["current"]["active"]["request"])
        self._token, self._spent = token, False
        self._lock = threading.Lock()
        self._client = urllib.request.build_opener(urllib.request.ProxyHandler({}), reader.Redirect())

    def _post(self, material):
        policy = resources.read_json("build.json")["cfworker"]
        selected = self._selected
        url = f"{policy['api']}/accounts/{selected['account']}/workers/workers/{selected['parent']}/previews?ignore_base_config=true"
        headers = {"Authorization": "Bearer " + self._token, "Accept": "application/json", "Accept-Encoding": "identity", "User-Agent": "wharf-lane", "Content-Type": "application/json", "Content-Length": str(len(material))}
        request = urllib.request.Request(url, material, headers, method="POST")
        with self._client.open(request, timeout=30) as response:
            if response.status not in (200, 201) or response.geturl() != url or response.headers.get("Content-Encoding") not in (None, "identity"):
                raise Refusal("cfworker create reply is not its exact untransformed endpoint")
            body = response.read(evidence.LIMIT + 1)
        reply = evidence.decode(body)
        if not isinstance(reply, dict) or reply.get("success") is not True or "errors" not in reply or reply["errors"] not in (None, []) or not isinstance(reply.get("result"), dict):
            raise Refusal("cfworker create reply is not a successful bounded object")
        return identified(reply["result"], self._plan)

    def create(self, observed, document, bodies):
        with self._lock:
            if self._spent:
                raise Refusal("cfworker creation was already attempted; no retry or recovery write")
            apply.qualified(self._plan["apply"], document, bodies)
            if discard.status(observed, self._selected, self._plan["policy"]) != self._plan["before"]:
                raise Refusal("cfworker create pre-write complete status moved")
            material = canonical.encode({"name": self._selected["name"]})
            self._spent = True
            try:
                created = self._post(material)
            except (OSError, urllib.error.URLError, TimeoutError, Refusal):
                return {"request": self._request, "acknowledged": False, "preview": None}
            return {"request": self._request, "acknowledged": True, "preview": created}
