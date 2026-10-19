import json

from lib.process.cfworker import mapping
from lib.process.domains import context
from tests.domains.test_domain import pins, reply
from tests.domains.test_base import observed, pins as base_pins
from tests.lib.process.test_cfworker import Response
from tests.lib.process.test_cfworker_observation import Observation


def policy(value):
    domain = pins()
    domain["worker"]["id"] = value["id"]
    return dict(value, domain=domain, base=base_pins())


def baseline():
    return dict(observed(), routes=[], environment_routes=[], schedules={"schedules": []})


def provider():
    fixture = Observation()
    fixture.setUp()
    fixture.policy = policy(fixture.policy)
    fixture.parent["references"]["domains"] = [{key: value for key, value in fixture.policy["domain"]["domain"].items() if key != "certificate_id"}]
    fixture.parent["deployed_on"] = "2026-10-19T00:00:00Z"
    api = fixture.client()
    native = api._client.open.side_effect
    held = context.projected(fixture.policy, mapping.selected(fixture.registered))
    def received(request, timeout):
        if "/workers/domains/" in request.full_url:
            value = {"success": True, "errors": [], "result": reply()}
        elif "/versions/" in request.full_url:
            value = {"success": True, "errors": [], "result": baseline()["version"]}
        elif "/previews" not in request.full_url and any(request.full_url.endswith(path) for path in ("/routes", "/schedules", "/deployments", "/deployments/" + base_pins()["deployment"])):
            key = "schedules" if request.full_url.endswith("/schedules") else "routes" if request.full_url.endswith("/routes") else "current" if request.full_url.endswith("/deployments") else "deployment"
            value = {"success": True, "errors": [], "result": baseline()[key]}
        else:
            with native(request, timeout) as response:
                value = json.load(response)
            entries = value["result"] if isinstance(value["result"], list) else [value["result"]]
            for item in entries:
                if item.get("urls"):
                    url = context.current(held, "preview") if "/deployments" not in request.full_url else context.address(held, "preview", item["id"], item["id"][:8])
                    item["urls"].append(url)
        return Response(json.dumps(value).encode(), request.full_url)
    api._client.open.side_effect = received
    return fixture, api
