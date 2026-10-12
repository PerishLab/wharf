import urllib.error
import urllib.request

from lib.content import resources
from lib.content.lane import codec
from lib.content.static import evidence
from lib.process.cfworker import discard, mapping, reader
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
        self._client = urllib.request.build_opener(urllib.request.ProxyHandler({}), reader.Redirect())

    def delete(self, observed):
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
