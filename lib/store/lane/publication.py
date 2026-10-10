from lib.content.lane import codec
from lib.refusal import Conflict, Refusal
from lib.store.lane import public


def binding(declared, record):
    declared = codec.read("declaration", declared)
    record = codec.read("record", record)
    entry = record["entry"]
    value = entry["value"]
    context = value["request"]["context"] if entry["kind"] == "operation" else value["context"]
    if context["requested"] != declared["target"] or context["adaptor"] != declared["adaptor"]:
        raise Refusal("lane public evidence differs from its exact declared target or adaptor")
    if entry["kind"] == "operation":
        assessed = value["assessment"]
        if assessed["capabilities"] != declared["capabilities"]:
            raise Refusal("lane public operation capabilities differ from declaration")
        authorization = assessed["conditions"]["authorization"]
        if authorization["state"] != "unknown" and authorization["evidence"]["authority"] != declared["authorities"]["authorization"]:
            raise Refusal("lane public operation names an undeclared authorization authority")
    return record


def authority(reader, declared, reference):
    declared = codec.read("declaration", declared)
    reference = codec.read("reference", reference)
    if not isinstance(reader, public.Reader) or reader.authority != declared["authorities"]["publication"] or reference["authority"] != reader.authority:
        raise Refusal("lane public evidence needs its declared prepared publication authority")
    return reference


def readback(reader, declared, reference, context):
    context = codec.read("context", context)
    reference = authority(reader, declared, reference)
    content = reader.read(reference)
    record = binding(declared, codec.verify(reference, reference, content))
    entry = record["entry"]
    actual = entry["value"]["request"]["context"] if entry["kind"] == "operation" else entry["value"]["context"]
    if actual != context:
        raise Refusal("lane public evidence does not observe the entire exact requested context")
    return record


def publish(writer, reader, declared, record):
    record = binding(declared, record)
    body = codec.encode("record", record)
    if len(body) > public.LIMIT:
        raise Refusal("lane public record exceeds its bounded byte contract")
    if not isinstance(reader, public.Reader):
        raise Refusal("lane publication requires an independently prepared public reader")
    reference = authority(reader, declared, reader.reference(codec.digest("record", record)))
    key = public.location(reference)
    try:
        writer.create(key, body, {"Content-Type": "application/json", "Cache-Control": "public, max-age=31536000, immutable"})
    except Conflict:
        if writer.get(key) != body:
            raise Refusal("lane immutable public key already names different private bytes")
    entry = record["entry"]
    context = entry["value"]["request"]["context"] if entry["kind"] == "operation" else entry["value"]["context"]
    verified = readback(reader, declared, reference, context)
    if codec.encode("record", verified) != body:
        raise Refusal("lane public readback differs from the exact publication bytes")
    return reference


def settle(reader, declared, reference, requested):
    requested = codec.read("request", requested)
    record = readback(reader, declared, reference, requested["context"])
    entry = record["entry"]
    if entry["kind"] != "operation" or entry["value"]["request"] != requested or entry["value"]["outcome"]["outcome"] != "applied":
        raise Refusal("lane public completion needs an applied observation of the exact request")
    operation = entry["value"]
    reason = codec.admit(requested, operation["assessment"], declared)
    if reason is not None:
        raise Refusal(f"lane declaration refuses public completion: {reason}")
    return operation
