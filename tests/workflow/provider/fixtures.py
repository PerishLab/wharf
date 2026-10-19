import json

from tests.domains import fixtures


def configure(environ, parent, app="crest"):
    policy = fixtures.policy({"id": environ["WHARF_LANE_PARENT_ID"], "subdomain": environ["WHARF_LANE_SUBDOMAIN"]})
    policy["domain"]["worker"]["name"] = parent["name"]
    policy["domain"]["app"] = app
    policy["domain"]["domain"]["hostname"] = "lane." + app + ".perish.uk"
    parent["domain"] = policy["domain"]
    parent["base"] = policy["base"]
    environ["WHARF_LANE_DOMAIN_PINS"] = json.dumps(policy["domain"])
    environ["WHARF_LANE_BASE_PINS"] = json.dumps(policy["base"])
    return policy
