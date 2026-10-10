import json

from lib.content.lane import codec, shape
from lib.refusal import Conflict, Refusal


def object_value(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise Refusal("lane store document requires exact fields")
    return {field: value[field] for field in fields}


def decode(body):
    if not isinstance(body, bytes) or len(body) > 65536:
        raise Refusal("lane store document exceeds its bounded byte contract")
    try:
        return json.loads(body.decode("utf-8"), object_pairs_hook=codec.unique)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise Refusal("lane store document is malformed") from error


def encode(value):
    body = shape.encode(value)
    if len(body) > 65536:
        raise Refusal("lane store document exceeds its bounded byte contract")
    return body


def location(target):
    target = codec.read("address", target)
    return f"lane/v1/{target['repository']}/{target['app']}/{target['lane']}"


def snapshot(bucket, key):
    body, etag = bucket.snapshot(key)
    if body is None:
        if etag is not None:
            raise Refusal("absent lane document cannot carry an ETag")
    elif not isinstance(etag, str) or not etag:
        raise Refusal("present lane document requires a versioned snapshot")
    return body, etag


def immutable(bucket, key, body):
    try:
        bucket.create(key, body)
    except Conflict:
        if bucket.get(key) != body:
            raise Refusal("lane immutable identity already names different bytes")
