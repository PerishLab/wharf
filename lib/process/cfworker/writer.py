import threading
import urllib.error
import urllib.request

from lib.content import resources
from lib.content.lane import codec
from lib.content.static import evidence
from lib.process.cfworker import apply, discard, mapping, observation, payload, reader, upload
from lib.refusal import Refusal


class Writer:
    def __init__(self, token, plan):
        if not isinstance(token, str) or not token or any(not 33 <= ord(char) <= 126 for char in token):
            raise Refusal("cfworker discard needs a separately prepared writer token")
        self._plan = discard.checked(plan)
        before = self._plan["before"]
        selected = before["preview"]
        if self._plan["current"]["phase"] != "reserved" or selected is None:
            raise Refusal("cfworker DELETE needs fresh reserved present selection; no recovery write")
        if selected["id"] in {item["name"] for item in before["previews"]}:
            raise Refusal("cfworker DELETE identity is ambiguous with a mutable Preview name")
        self._token = token
        self._spent = False
        self._lock = threading.Lock()
        self._client = urllib.request.build_opener(urllib.request.ProxyHandler({}), reader.Redirect())

    def delete(self, observed):
        with self._lock:
            return self._delete(observed)

    def _delete(self, observed):
        plan = self._plan
        selected = mapping.selected(plan["registration"])
        if discard.status(observed, selected, plan["policy"]) != plan["before"]:
            raise Refusal("cfworker discard pre-write status moved")
        if self._spent:
            raise Refusal("cfworker discard writer was already attempted; no retry")
        policy = resources.read_json("build.json")["cfworker"]
        identity = plan["before"]["preview"]["id"]
        url = f"{policy['api']}/accounts/{selected['account']}/workers/workers/{selected['parent']}/previews/{identity}"
        headers = {"Authorization": "Bearer " + self._token, "Accept": "application/json", "Accept-Encoding": "identity", "User-Agent": "wharf-lane"}
        request = urllib.request.Request(url, headers=headers, method="DELETE")
        result = {"request": codec.digest("request", plan["current"]["active"]["request"]), "acknowledged": False}
        self._spent = True
        try:
            with self._client.open(request, timeout=30) as response:
                if response.status != 200 or response.geturl() != url or response.headers.get("Content-Encoding") not in (None, "identity"):
                    raise Refusal("cfworker discard reply is not its exact untransformed endpoint")
                body = response.read(evidence.LIMIT + 1)
            reply = evidence.decode(body)
            if not isinstance(reply, dict) or reply.get("success") is not True or "errors" not in reply or reply["errors"] not in (None, []) or "result" not in reply or reply["result"] not in (None, {}):
                raise Refusal("cfworker discard reply is not a successful acknowledgement")
        except (OSError, urllib.error.URLError, TimeoutError, Refusal):
            return result
        return dict(result, acknowledged=True)


class Deployer:
    def __init__(self, token, plan, completion):
        if not isinstance(token, str) or not token or any(not 33 <= ord(char) <= 126 for char in token):
            raise Refusal("cfworker deployment needs a separately prepared writer credential")
        self._plan = apply.deployment_checked(plan)
        self._selected = mapping.selected(self._plan["apply"]["registration"])
        self._request = codec.digest("request", self._plan["apply"]["current"]["active"]["request"])
        self._token, self._completion = token, upload.jwt(completion)
        self._spent = False
        self._status = None
        self._lock = threading.Lock()
        self._client = urllib.request.build_opener(urllib.request.ProxyHandler({}), reader.Redirect())

    def _post(self, material):
        policy = resources.read_json("build.json")["cfworker"]
        selected = self._selected
        preview = self._plan["before"]["preview"]
        url = f"{policy['api']}/accounts/{selected['account']}/workers/workers/{selected['parent']}/previews/{preview['id']}/deployments?ignore_defaults=true"
        headers, body = material
        headers = dict(headers, Authorization="Bearer " + self._token, Accept="application/json", **{"Accept-Encoding": "identity", "User-Agent": "wharf-lane"})
        request = urllib.request.Request(url, body, headers, method="POST")
        self._status = None
        with self._client.open(request, timeout=30) as response:
            self._status = response.status
            if response.status not in (200, 201):
                raise upload.Protocol('response-status')
            if response.geturl() != url:
                raise upload.Protocol('response-origin')
            if response.headers.get("Content-Encoding") not in (None, "identity"):
                raise upload.Protocol('response-encoding')
            body = response.read(evidence.LIMIT + 1)
        try:
            reply = evidence.decode(body)
        except Refusal:
            raise upload.Protocol('response-body') from None
        if not isinstance(reply, dict):
            raise upload.Protocol('envelope-type')
        if reply.get("success") is not True:
            raise upload.Protocol('envelope-success')
        if "errors" not in reply:
            raise upload.Protocol('envelope-errors-missing')
        if reply["errors"] not in (None, []):
            raise upload.Protocol('envelope-errors')
        value = reply.get("result")
        if not isinstance(value, dict):
            raise upload.Protocol('result-shape')
        try:
            deployed = observation.deployment(value, preview, value.get("id"))
        except Refusal:
            raise upload.Protocol('deployment-identity') from None
        if deployed in self._plan["before"]["deployments"]:
            raise upload.Protocol('deployment-retained')
        return deployed

    def deploy(self, observed, document, bodies):
        with self._lock:
            if self._spent:
                raise Refusal("cfworker deployment was already attempted; no retry or recovery write")
            plan = self._plan
            if apply.deployment_status(observed, self._selected, plan["policy"]) != plan["before"]:
                raise Refusal("cfworker pre-write deployment catalog or Preview status moved")
            material = payload.deployment(plan["apply"], document, bodies, self._completion)
            self._spent = True
            try:
                deployed = self._post(material)
            except (OSError, urllib.error.URLError, TimeoutError, Refusal) as error:
                upload.diagnostic('deployment-response', error, self._status)
                return {"request": self._request, "acknowledged": False, "deployment": None}
            return {"request": self._request, "acknowledged": True, "deployment": deployed}
