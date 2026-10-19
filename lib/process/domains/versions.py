import http.client
import urllib.error
import urllib.request

from lib.content import resources
from lib.content.static import evidence
from lib.process.cfworker import inventory, mapping, observation
from lib.process.domains import context, identity
from lib.process.domains.transport import Redirect
from lib.refusal import Refusal
from lib.store.lane import documents


def project(value, parent):
    requested = value.get("id") if isinstance(value, dict) else None
    if not isinstance(requested, str) or not observation.DEPLOYMENT.fullmatch(requested):
        raise Refusal("cfworker Worker version needs its exact full UUID")
    prefix = requested[:8]
    expected = {observation.address(parent, requested, prefix), identity.deployment(parent["domain"]["domain"]["hostname"], prefix)}
    urls = value.get("urls")
    if not isinstance(urls, list) or any(not isinstance(url, str) for url in urls) or len(urls) != 2 or set(urls) != expected:
        raise Refusal("cfworker Worker version URLs differ from exact prepared routing")
    return {"id": requested, "prefix": prefix}


def checked(values, parent):
    if not isinstance(values, list) or not 1 <= len(values) <= inventory.SIZE:
        raise Refusal("cfworker Worker versions need a bounded complete catalog")
    identities, prefixes, held = set(), set(), []
    for value in values:
        value = documents.object_value(value, ("id", "prefix"))
        requested, prefix = value["id"], value["prefix"]
        if not isinstance(requested, str) or not observation.DEPLOYMENT.fullmatch(requested) or not isinstance(prefix, str) or not identity.PREFIX.fullmatch(prefix) or prefix != requested[:8]:
            raise Refusal("cfworker Worker version has a malformed UUID or prefix")
        if requested in identities or prefix in prefixes:
            raise Refusal("cfworker Worker version namespace is ambiguous")
        identities.add(requested)
        prefixes.add(prefix)
        held.append(dict(value))
    if parent["base"]["version"] not in identities:
        raise Refusal("cfworker complete Worker versions lack the pinned canonical base")
    return held


class Reader:
    def __init__(self, token, registered, policy):
        if not isinstance(token, str) or not token or any(not 33 <= ord(char) <= 126 for char in token):
            raise Refusal("cfworker Worker versions need a prepared read-only token")
        self._parent = context.projected(policy, mapping.selected(registered))
        if "domain" not in self._parent:
            raise Refusal("cfworker Worker version catalog requires explicit domain and base pins")
        self._token = token
        self._client = urllib.request.build_opener(urllib.request.ProxyHandler({}), Redirect())

    def page(self, number):
        if type(number) is not int or not 1 <= number <= inventory.PAGES:
            raise Refusal("cfworker Worker versions need one bounded exact page number")
        root = resources.read_json("build.json")["cfworker"]["api"]
        url = root + "/accounts/" + self._parent["account"] + "/workers/workers/" + self._parent["id"] + "/versions?per_page=50&page=" + str(number)
        headers = {"Authorization": "Bearer " + self._token, "Accept": "application/json", "Accept-Encoding": "identity", "User-Agent": "wharf-lane"}
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with self._client.open(request, timeout=30) as response:
                if response.status != 200 or response.geturl() != url or response.headers.get("Content-Encoding") not in (None, "identity"):
                    raise Refusal("cfworker Worker version read differs from its exact untransformed endpoint")
                body = response.read(evidence.LIMIT + 1)
        except (OSError, urllib.error.URLError, TimeoutError, http.client.HTTPException) as error:
            raise Refusal("cfworker Worker version read failed; no fallback or repair inferred") from error
        return evidence.decode(body)

    def read(self):
        values, total, width, pages = inventory.page(self.page(1), 1)
        if total > inventory.SIZE or width != 50:
            raise Refusal("cfworker Worker version catalog exceeds its supported complete bound")
        held = []
        for number in range(1, pages + 1):
            if number > 1:
                values, count, size, observed = inventory.page(self.page(number), number)
                if (count, size, observed) != (total, width, pages):
                    raise Refusal("cfworker Worker version pagination moved during observation")
            held.extend(project(value, self._parent) for value in values)
        return checked(sorted(held, key=lambda item: item["id"]), self._parent)
