import http.client
import urllib.error
import urllib.request
from pathlib import PurePosixPath

from lib.content import resources
from lib.content.lane import codec
from lib.content.static import assets, evidence
from lib.process.cfworker import apply, discard, inventory, mapping, observation, payload
from lib.process.domains import baseline, context, reader as domains, routing
from lib.process.domains.transport import Redirect
from lib.refusal import Refusal
from lib.store.lane import documents, publication, registration, state

def values(headers, name):
    if hasattr(headers, "get_all"):
        result = headers.get_all(name, [])
    else:
        value = headers.get(name)
        result = [] if value is None else [value]
    if not isinstance(result, list) or any(not isinstance(item, str) for item in result):
        raise Refusal("cfworker content readback has malformed headers")
    return result


def policy(headers, name):
    required = {"Content-Security-Policy": assets.CSP, "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"}
    if any(values(headers, key) != [value] for key, value in required.items()):
        raise Refusal("cfworker content readback differs from vetted security policy")
    robots = [item.strip().lower() for value in values(headers, "X-Robots-Tag") for item in value.split(",")]
    if set(robots) != {"noindex", "nofollow"}:
        raise Refusal("cfworker content readback differs from vetted indexing policy")
    types = values(headers, "Content-Type")
    parts = [item.strip().lower() for item in types[0].split(";")] if len(types) == 1 else []
    if not parts or parts[0] != payload.MIME.get(PurePosixPath(name).suffix.lower()) or parts[1:] not in ([], ["charset=utf-8"], ['charset="utf-8"']):
        raise Refusal("cfworker content readback has a different static media type")
    if values(headers, "Content-Encoding") not in ([], ["identity"]) or values(headers, "Content-Range"):
        raise Refusal("cfworker content readback is transformed or partial")


def inspection(value):
    value = documents.object_value(documents.decode(documents.encode(value)), ("registration", "current", "inspection", "policy"))
    registered = registration.checked(value["registration"])
    mapping.selected(registered)
    current = state.checked(value["current"], registered)
    active = state.reservation(value["inspection"])
    registration.admitted(registered, active["request"], active["assessment"])
    if active["request"]["intent"]["action"] != "inspect" or active["request"]["context"]["revision"] != current["revision"]:
        raise Refusal("cfworker inspection needs an exact current inspect request")
    conditions = active["assessment"]["conditions"]
    if current["active"] is not None or conditions["writer"] != "idle" or conditions["observed"] != current["observed"]:
        raise Refusal("cfworker inspection cannot claim historical material while a writer is uncertain")
    return {"registration": registered, "current": current, "inspection": active, "policy": context.prepared(value["policy"], mapping.selected(registered))}


def published(plan, reader, reference):
    plan = inspection(plan)
    active = plan["inspection"]
    operation = publication.settle(reader, plan["registration"]["declaration"], reference, active["request"])
    current = plan["current"]
    material = {"state": "absent"} if current["observed"]["state"] == "absent" else current["last"]["outcome"]["material"]
    if operation["assessment"] != active["assessment"] or operation["outcome"]["writer"] != "idle" or operation["outcome"]["material"] != material:
        raise Refusal("cfworker public inspection changed the exact assessment, writer or material")
    return operation


class Readback:
    def __init__(self, plan, observed, routed):
        held = apply.deployment_readable(plan)
        routed = documents.object_value(routed, ("status", "catalogs", "deployment"))
        self._deployment = apply.deployment_confirm(held, observed, routed["deployment"])
        self._plan = held["apply"]
        self._parent = held["before"]["parent"]
        self._routing = documents.decode(documents.encode(routed))
        self._origin = routing.checked(self._routing, observed, self._deployment)
        self._client = urllib.request.build_opener(urllib.request.ProxyHandler({}), Redirect())

    def current(self):
        self._origin = context.current(self._parent, mapping.selected(self._plan["registration"])["name"])
        return self

    def _get(self, route, selected):
        name, expected = selected
        url = self._origin + route
        request = urllib.request.Request(url, headers={"Accept-Encoding": "identity", "Cache-Control": "no-cache", "User-Agent": "wharf-lane"}, method="GET")
        try:
            with self._client.open(request, timeout=30) as response:
                if response.status != 200 or response.geturl() != url:
                    raise Refusal("cfworker content readback is not its exact prepared endpoint")
                policy(response.headers, name)
                lengths = values(response.headers, "Content-Length")
                if lengths and lengths != [str(len(expected))]:
                    raise Refusal("cfworker content readback differs from exact asset length")
                body = response.read(len(expected) + 1)
                if not isinstance(body, bytes) or body != expected:
                    raise Refusal("cfworker content readback differs from exact vetted bytes")
        except (OSError, urllib.error.URLError, TimeoutError, http.client.HTTPException) as error:
            raise Refusal("cfworker content readback failed; no deployment retry or fallback") from error

    def read(self, document, bodies):
        plan, files = apply.readable_content(self._plan, document, bodies)
        routes = payload.routes(files)
        for route, selected in routes.items():
            self._get(route, selected)
        if apply.readable_content(self._plan, document, bodies)[1] != files:
            raise Refusal("cfworker handoff changed during deployed content readback")
        return {"request": codec.digest("request", plan["current"]["active"]["request"]), "artifact": plan["artifact"],
                "deployment": dict(self._deployment), "routing": documents.decode(documents.encode(self._routing)),
                "address": self._origin, "files": len(routes)}


class Inspection(Readback):
    def __init__(self, plan, observed):
        self._plan = inspection(plan)
        selected = mapping.selected(self._plan["registration"])
        self._observed = discard.status(documents.decode(documents.encode(observed)), selected, self._plan["policy"])
        if (self._observed["preview"] is None) != (self._plan["current"]["observed"]["state"] == "absent"):
            raise Refusal("cfworker inspection control-plane presence differs from settled material")
        self._origin = context.current(self._observed["parent"], selected["name"])
        self._client = urllib.request.build_opener(urllib.request.ProxyHandler({}), Redirect())

    def read(self, document, bodies):
        plan = inspection(self._plan)
        material = {"state": "absent"}
        if self._observed["preview"] is not None:
            document, files = apply.content(document, bodies)
            artifact = apply.identity(document)
            if artifact != plan["current"]["last"]["outcome"]["material"]["artifact"]:
                raise Refusal("cfworker inspection handoff differs from settled content")
            apply.matches({"registration": plan["registration"], "workload": document["key"], "artifact": artifact}, document)
            if artifact["source"]["repository"] != plan["current"]["target"]["repository"]:
                raise Refusal("cfworker inspection handoff belongs to another repository")
            routes = payload.routes(files)
            for route, selected in routes.items():
                self._get(route, selected)
            if apply.content(document, bodies)[1] != files:
                raise Refusal("cfworker handoff changed during current content inspection")
            material = {"state": "present", "artifact": artifact}
        elif document is not None or bodies is not None:
            raise Refusal("cfworker absent inspection consumes no content or checkout")
        return codec.read("operation", dict(plan["inspection"], outcome={"outcome": "applied", "material": material, "writer": "idle"}))


class Reader:
    def __init__(self, token, registered):
        if not isinstance(token, str) or not token or any(not 33 <= ord(char) <= 126 for char in token):
            raise Refusal("static provider needs a prepared read-only token")
        self._token = token
        self._registration = registration.checked(registered)
        self._registered = mapping.selected(registered)
        self._client = urllib.request.build_opener(urllib.request.ProxyHandler({}), Redirect())

    def get(self, suffix):
        reply = self.reply(suffix)
        if not isinstance(reply.get("result"), dict):
            raise Refusal("static provider read is not a successful object observation")
        return reply["result"]

    def reply(self, suffix, selected=None, namespace=None):
        name = self._registered["name"]
        if namespace is not None and selected is None:
            raise Refusal("cfworker namespace read needs an exact contextual Preview")
        if selected is not None:
            selected = documents.object_value(selected, ("id", "name", "parent"))
            if namespace is not None:
                routing.context(namespace, selected, self._registered)
            elif selected["name"] != name:
                raise Refusal("cfworker bound catalog differs from its selected Preview")
            if selected["id"] == selected["name"]:
                raise Refusal("cfworker bound catalog differs from its exact unambiguous selected Preview")
            observation.identity(selected["parent"])
            name = observation.identity(selected["id"])
        listing = tuple(f"/previews?page={number}&per_page={inventory.SIZE}" for number in range(1, inventory.PAGES + 1))
        history = tuple(f"/previews/{name}/deployments?page={number}&per_page={inventory.SIZE}" for number in range(1, inventory.PAGES + 1))
        allowed = history if selected is not None else ("", f"/previews/{name}", *listing, *history)
        if namespace is not None and suffix not in history:
            raise Refusal("cfworker sibling namespace read admits only fixed catalog pages")
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

    def deployment_page(self, number, selected):
        if type(number) is not int or not 1 <= number <= inventory.PAGES:
            raise Refusal("cfworker deployment catalog needs one bounded exact page number")
        selected = documents.object_value(selected, ("id", "name", "parent"))
        return self.reply(f"/previews/{selected['id']}/deployments?page={number}&per_page={inventory.SIZE}", selected)

    def namespace_page(self, number, context):
        if type(number) is not int or not 1 <= number <= inventory.PAGES:
            raise Refusal("cfworker namespace catalog needs one bounded exact page number")
        if not isinstance(context, tuple) or len(context) != 2:
            raise Refusal("cfworker namespace catalog needs exact Preview and parent context")
        selected, namespace = context
        selected = documents.object_value(selected, ("id", "name", "parent"))
        return self.reply(f"/previews/{selected['id']}/deployments?page={number}&per_page={inventory.SIZE}", selected, namespace)

    def status(self, policy):
        policy = context.prepared(policy, self._registered)
        held = self.qualified_parent(policy)
        catalog = inventory.read(self, held)
        selected = next((item for item in catalog if item["name"] == self._registered["name"]), None)
        if selected is not None and context.resource(self.preview(), self._registered, held) != selected:
            raise Refusal("cfworker selected Preview moved after inventory")
        if inventory.read(self, held) != catalog:
            raise Refusal("cfworker Preview inventory moved during status")
        if self.qualified_parent(policy) != held:
            raise Refusal("cfworker parent changed during status")
        return {"parent": held, "previews": catalog, "preview": selected}

    def qualified_parent(self, policy):
        policy = context.prepared(policy, self._registered)
        if "domain" not in policy:
            return observation.parent(self.parent(), self._registered, policy)
        domain = domains.Reader(self._token, self._registration, policy["domain"]).read()
        base = baseline.Reader(self._token, self._registration, policy).read()
        value = {"policy": {key: policy[key] for key in ("id", "subdomain", "base")},
                 "parent": self.parent(), "domain": domain, "base": base}
        return context.observed(self._registration, policy, value)

    def history(self, policy):
        before = self.status(policy)
        selected = before["preview"]
        if selected is None:
            raise Refusal("cfworker deployment catalog needs a present selected Preview")
        if selected["id"] in {item["name"] for item in before["previews"]}:
            raise Refusal("cfworker deployment catalog cannot use an ambiguous Preview ID/name alias")
        catalog = inventory.deployments(self, selected)
        if inventory.deployments(self, selected) != catalog:
            raise Refusal("cfworker retained deployment catalog moved during observation")
        if self.status(policy) != before:
            raise Refusal("cfworker parent, selected Preview or siblings moved during deployment observation")
        return dict(before, deployments=catalog)

    def observe(self, policy, identity):
        if not isinstance(identity, str) or not observation.DEPLOYMENT.fullmatch(identity):
            raise Refusal("cfworker observation needs an exact deployment identity")
        policy = context.prepared(policy, self._registered)
        before = self.history(policy)
        selected = before["preview"]
        if context.preview(self.preview(), self._registered, before["parent"]) != selected:
            raise Refusal("cfworker exact deployed Preview differs from complete history")
        catalogs = routing.read(self, before)
        raw = self.reply(f"/previews/{selected['id']}/deployments/{identity}", selected).get("result")
        deployed = observation.endpoint(raw, selected, identity)
        route = routing.project(raw, selected, before["parent"])
        catalog = next(item for item in catalogs if item["preview"] == selected)
        if route not in catalog["deployments"]:
            raise Refusal("cfworker exact deployment route differs from its complete retained catalog")
        if routing.read(self, before) != catalogs or self.history(policy) != before:
            raise Refusal("cfworker complete routing namespace moved during exact observation")
        value = {"status": before, "catalogs": catalogs, "deployment": deployed}
        routing.checked(value, before, deployed)
        return value

    def preview(self):
        return self.get(f"/previews/{self._registered['name']}")

    def deployment(self, identity):
        if not isinstance(identity, str) or not observation.DEPLOYMENT.fullmatch(identity):
            raise Refusal("static provider deployment requires an exact UUID, not latest")
        return self.get(f"/previews/{self._registered['name']}/deployments/{identity}")
