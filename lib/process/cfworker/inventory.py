from lib.process.cfworker import observation
from lib.refusal import Refusal
from lib.store.lane import documents

SIZE = 100
PAGES = 10
ITEMS = 1000


def page(reply, number):
    if type(number) is not int or not 1 <= number <= PAGES:
        raise Refusal("cfworker inventory needs one bounded exact page number")
    if not isinstance(reply, dict) or reply.get("success") is not True or "errors" not in reply or reply["errors"] not in (None, []) or not isinstance(reply.get("result"), list) or not isinstance(reply.get("result_info"), dict):
        raise Refusal("cfworker inventory needs a successful paginated list")
    values = reply["result"]
    info = reply["result_info"]
    for key in ("page", "count", "total_count", "per_page"):
        if type(info.get(key)) is not int or info[key] < 0:
            raise Refusal("cfworker inventory page metadata is missing or malformed")
    total = info["total_count"]
    size = info["per_page"]
    if info["page"] != number or not 1 <= size <= SIZE or total > ITEMS:
        raise Refusal("cfworker inventory exceeds supported page or item bounds")
    pages = max(1, (total + size - 1) // size)
    expected = min(size, max(0, total - (number - 1) * size))
    if pages > PAGES or number > pages or info["count"] != len(values) or len(values) != expected:
        raise Refusal("cfworker inventory page is incomplete or inconsistent")
    if "total_pages" in info and (type(info["total_pages"]) is not int or info["total_pages"] not in ({0, 1} if total == 0 else {pages})):
        raise Refusal("cfworker inventory total pages disagree")
    return values, total, size, pages


def read(api, parent):
    values, total, size, pages = page(api.page(1), 1)
    catalog = []
    names = set()
    identities = set()
    for number in range(1, pages + 1):
        if number > 1:
            values, observed, width, count = page(api.page(number), number)
            if (observed, width, count) != (total, size, pages):
                raise Refusal("cfworker inventory pagination moved during observation")
        for value in values:
            item = project(value, parent)
            if item["name"] in names or item["id"] in identities:
                raise Refusal("cfworker inventory repeats a Preview name or identity")
            names.add(item["name"])
            identities.add(item["id"])
            catalog.append(item)
    if len(catalog) != total:
        raise Refusal("cfworker inventory is incomplete")
    return sorted(catalog, key=lambda item: item["name"])


def project(value, parent):
    name = value.get("name") if isinstance(value, dict) else None
    if not isinstance(name, str) or not observation.LABEL.fullmatch(name) or not observation.LABEL.fullmatch(name + "-" + parent["name"]):
        raise Refusal("cfworker inventory has an unsupported Preview name")
    return observation.resource(value, {"name": name}, parent)


def deployments(api, selected):
    values, total, size, pages = page(api.deployment_page(1, selected), 1)
    if total > SIZE:
        raise Refusal("cfworker retained deployment catalog exceeds its native bound")
    catalog = []
    for number in range(1, pages + 1):
        if number > 1:
            values, observed, width, count = page(api.deployment_page(number, selected), number)
            if (observed, width, count) != (total, size, pages):
                raise Refusal("cfworker deployment pagination moved during observation")
        for value in values:
            identity = value.get("id") if isinstance(value, dict) else None
            catalog.append(observation.endpoint(value, selected, identity))
    if len(catalog) != total:
        raise Refusal("cfworker retained deployment catalog is incomplete")
    return history(catalog, selected)


def history(values, selected):
    if not isinstance(values, list) or len(values) > SIZE:
        raise Refusal("cfworker retained deployments need one bounded complete projection")
    catalog, identities = [], set()
    for value in values:
        value = documents.object_value(value, ("id", "preview", "parent"))
        identity = value["id"]
        if not isinstance(identity, str) or not observation.DEPLOYMENT.fullmatch(identity) or value["preview"] != selected["id"] or value["parent"] != selected["parent"] or identity in identities:
            raise Refusal("cfworker retained deployment identity is malformed, repeated or foreign")
        identities.add(identity)
        catalog.append(dict(value))
    return sorted(catalog, key=lambda item: item["id"])
