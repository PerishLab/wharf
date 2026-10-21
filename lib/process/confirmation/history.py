from lib.content.lane import preview
from lib.refusal import Refusal

COMMIT = "49b24d1de8502260a79713b108e3f45683c8d81f"
TREE = "ec0466c07c87208f21674203c193413a825329ae"
NAMES = (
    "[verify] request lane / [verify] request lane",
    "[verify] request lane / [build] product lane",
    "[verify] request lane / [bind] request lane",
    "[verify] request lane / [verify] product lane",
    "[read] cfworker lane / [verify] product lane",
    "[deploy] cfworker lane / [deploy] cfworker lane",
    "[deploy] cfworker disposal / [deploy] cfworker disposal",
    "[record] evidence lane / [record] evidence lane",
    "[record] evidence lane / [record] state lane",
)


def supported(intent, context):
    if intent["operation"] != "apply" or context["commit"] != COMMIT:
        raise Refusal("lane confirmation original source contract is not independently qualified")


def qualify(api, repository, context):
    preview.matches(context["commit"], preview.HEX[40], "original source commit")
    if context["commit"] != COMMIT:
        raise Refusal("lane confirmation original source contract is unsupported")
    value = api.commit(repository, COMMIT)
    if not isinstance(value, dict) or value.get("sha") != COMMIT or not isinstance(value.get("tree"), dict) or value["tree"].get("sha") != TREE:
        raise Refusal("lane confirmation original source commit/tree differs from its reviewed contract")
    return {"commit": COMMIT, "tree": TREE, "graph": "wharf.lane.original-graph/v1"}
