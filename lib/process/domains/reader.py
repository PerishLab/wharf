import http.client
import urllib.error
import urllib.request

from lib.content import resources
from lib.content.static import evidence
from lib.process.cfworker.reader import Redirect
from lib.process.domains import identity
from lib.refusal import Refusal
from lib.store.lane import registration


class Reader:
    def __init__(self, token, registered, pins):
        if not isinstance(token, str) or not token or any(not 33 <= ord(char) <= 126 for char in token):
            raise Refusal("cfworker custom domain reader needs a prepared read-only token")
        self._registered = registration.checked(registered)
        self._pins = identity.prepare(self._registered, pins)
        self._token = token
        self._client = urllib.request.build_opener(urllib.request.ProxyHandler({}), Redirect())

    def read(self):
        pins = identity.prepare(self._registered, self._pins)
        base = resources.read_json("build.json")["cfworker"]["api"]
        url = base + "/accounts/" + pins["account"] + "/workers/domains/" + pins["domain"]["id"]
        headers = {"Authorization": "Bearer " + self._token, "Accept": "application/json", "Accept-Encoding": "identity", "User-Agent": "wharf-lane"}
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with self._client.open(request, timeout=30) as response:
                if response.status != 200 or response.geturl() != url or response.headers.get("Content-Encoding") not in (None, "identity"):
                    raise Refusal("cfworker custom domain read differs from its exact untransformed endpoint")
                body = response.read(evidence.LIMIT + 1)
        except (OSError, urllib.error.URLError, TimeoutError, http.client.HTTPException) as error:
            raise Refusal("cfworker custom domain read failed; no absence, readiness or fallback inferred") from error
        value = evidence.decode(body)
        if not isinstance(value, dict) or value.get("success") is not True or "errors" not in value or value["errors"] not in (None, []):
            raise Refusal("cfworker custom domain read is not a successful observation")
        return identity.observe(self._registered, pins, value.get("result"))
