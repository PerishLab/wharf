from lib.content.lane import codec, shape
from lib.content.static import declaration
from lib.refusal import Refusal
from lib.store.lane import documents

FIELDS = ("schema", "declaration", "mapping", "generation", "enabled")


def checked(value):
    value = documents.object_value(value, FIELDS)
    if value["schema"] != "wharf.lane.registration/v1" or type(value["enabled"]) is not bool:
        raise Refusal("lane registration schema or enabled flag is malformed")
    declared = codec.read("declaration", value["declaration"])
    declaration.name(declared["target"]["lane"])
    declaration.binding({key: declared[key] for key in ("adaptor", "capabilities", "authorities")})
    mapping = documents.object_value(value["mapping"], ("provider", "access", "account", "resource"))
    declaration.mapping(mapping)
    return dict(value, declaration=declared, mapping=mapping, generation=codec.read("digest", value["generation"]))


def location(target):
    return documents.location(target) + "/registration.json"


def read(bucket, target):
    target = codec.read("address", target)
    body, etag = documents.snapshot(bucket, location(target))
    if body is None:
        raise Refusal("lane registration is absent; no target inferred")
    value = checked(documents.decode(body))
    if value["declaration"]["target"] != target:
        raise Refusal("lane registration belongs to another exact target")
    return value, etag


def write(bucket, value, etag):
    value = checked(value)
    if etag is not None and (not isinstance(etag, str) or not etag):
        raise Refusal("lane registration update requires an exact ETag")
    bucket.swap(location(value["declaration"]["target"]), documents.encode(value), etag)


def admitted(value, requested, assessed):
    value = checked(value)
    if not value["enabled"]:
        raise Refusal("lane registration is disabled")
    reason = codec.admit(requested, assessed, value["declaration"])
    if reason is not None:
        raise Refusal(f"lane registered request was not admitted: {reason}")
    return value


def digest(value):
    return shape.digest(checked(value))


def confirm(bucket, value):
    value = checked(value)
    current, _ = read(bucket, value["declaration"]["target"])
    if current != value:
        raise Refusal("lane registration moved during conditional coordination")
