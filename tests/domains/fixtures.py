import copy
import json

from lib.process.cfworker import mapping
from lib.process.domains import context
from tests.domains.test_domain import pins, reply
from tests.lib.process.test_cfworker import Response
from tests.lib.process.test_cfworker_observation import Observation


def policy(value):
    domain = pins()
    domain["worker"]["id"] = value["id"]
    return dict(value, domain=domain)


def provider():
    fixture = Observation()
    fixture.setUp()
    fixture.policy = policy(fixture.policy)
    fixture.parent["references"]["domains"] = [copy.deepcopy(fixture.policy["domain"]["domain"])]
    api = fixture.client()
    native = api._client.open.side_effect
    held = context.projected(fixture.policy, mapping.selected(fixture.registered))
    def received(request, timeout):
        if "/workers/domains/" in request.full_url:
            value = {"success": True, "errors": [], "result": reply()}
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
