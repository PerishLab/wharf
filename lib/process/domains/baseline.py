import http.client
import urllib.error
import urllib.request

from lib.content import resources
from lib.content.static import evidence
from lib.process.cfworker import mapping, observation
from lib.process.domains import base, context
from lib.process.domains.transport import Redirect
from lib.refusal import Refusal


class Reader:
    def __init__(self, token, registered, policy):
        if not isinstance(token, str) or not token or any(not 33 <= ord(char) <= 126 for char in token):
            raise Refusal("cfworker base reader requires a prepared read-only token")
        self._selected = mapping.selected(registered)
        self._policy = context.prepared(policy, self._selected)
        if "base" not in self._policy:
            raise Refusal("cfworker base reader requires exact trusted domain and base pins")
        self._token = token
        self._client = urllib.request.build_opener(urllib.request.ProxyHandler({}), Redirect())

    def _paths(self):
        selected = self._selected
        pins = base.prepared(self._policy["base"])
        root = "/accounts/" + selected["account"] + "/workers/"
        script = root + "scripts/" + selected["parent"]
        worker = observation.identity(self._policy["id"])
        return {"version": root + "workers/" + worker + "/versions/" + pins["version"] + "?include=modules",
                "deployment": script + "/deployments/" + pins["deployment"],
                "current": script + "/deployments", "routes": script + "/routes",
                "environment_routes": root + "services/" + selected["parent"] + "/environments/production/routes",
                "schedules": script + "/schedules"}

    def _get(self, key):
        paths = self._paths()
        if not isinstance(key, str) or key not in paths:
            raise Refusal("cfworker base reader admits only fixed exact identity endpoints")
        url = resources.read_json("build.json")["cfworker"]["api"] + paths[key]
        headers = {"Authorization": "Bearer " + self._token, "Accept": "application/json", "Accept-Encoding": "identity", "User-Agent": "wharf-lane"}
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with self._client.open(request, timeout=30) as response:
                if response.status != 200 or response.geturl() != url or response.headers.get("Content-Encoding") not in (None, "identity"):
                    raise Refusal("cfworker base read differs from its exact untransformed endpoint")
                body = response.read(evidence.LIMIT + 1)
        except (OSError, urllib.error.URLError, TimeoutError, http.client.HTTPException) as error:
            raise Refusal("cfworker base read failed; no absence, fallback or repair inferred") from error
        value = evidence.decode(body)
        if not isinstance(value, dict) or value.get("success") is not True or "errors" not in value or value["errors"] not in (None, []):
            raise Refusal("cfworker base read is not a successful explicit observation")
        if key in ("routes", "environment_routes") and value.get("result_info") not in (None, {}):
            raise Refusal("cfworker base associated routes have unsupported pagination")
        return value.get("result")

    def read(self):
        value = {key: self._get(key) for key in self._paths()}
        base.isolated(self._policy["base"], value)
        return value
