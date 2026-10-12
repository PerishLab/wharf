import base64
import hashlib
import re
from pathlib import PurePosixPath

from lib.content import canonical
from lib.content.lane import codec
from lib.content.static import assets, evidence
from lib.process.cfworker import apply
from lib.refusal import Refusal

CONFIG = {"html_handling": "auto-trailing-slash", "not_found_handling": "404-page", "run_worker_first": False}
COMPLETION = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def digest(name, body):
    extension = PurePosixPath(name).suffix[1:]
    return hashlib.sha256(base64.b64encode(body) + extension.encode("ascii")).hexdigest()[:32]


def manifest(plan, document, bodies):
    _, files = apply.qualified(plan, document, bodies)
    return catalog(files)


def catalog(files):
    result, identities = {}, {}
    for name, body in sorted(files.items()):
        if name == "_headers":
            continue
        held = digest(name, body)
        identity = (body, PurePosixPath(name).suffix)
        if held in identities and identities[held] != identity:
            raise Refusal("cfworker asset cache key collides across different byte or extension identities")
        identities[held] = identity
        result["/" + name] = {"hash": held, "size": len(body)}
    if len(canonical.encode({"manifest": result})) > evidence.LIMIT:
        raise Refusal("cfworker asset manifest exceeds its bounded request budget")
    return result


def deployment(plan, document, bodies, completion):
    plan, files = apply.qualified(plan, document, bodies)
    catalog(files)
    if not isinstance(completion, str) or len(completion) > 8192 or not COMPLETION.fullmatch(completion):
        raise Refusal("cfworker deployment needs a private prepared asset completion JWT")
    requested = plan["current"]["active"]["request"]
    metadata = canonical.encode({"assets": {"jwt": completion, "config": dict(CONFIG)},
                                 "annotations": {"workers/commit_sha": plan["artifact"]["source"]["commit"],
                                                 "workers/message": codec.digest("request", requested),
                                                 "workers/tag": plan["artifact"]["digest"]}})
    boundary = "wharf-" + codec.digest("request", requested)
    headers = files["_headers"]
    if headers != assets.HEADERS or boundary.encode() in metadata or boundary.encode() in headers:
        raise Refusal("cfworker multipart policy or boundary disagrees with exact prepared content")
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"\r\nContent-Type: application/json\r\n\r\n'.encode()
            + metadata + f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="_headers"\r\nContent-Type: text/plain\r\n\r\n'.encode()
            + headers + f"\r\n--{boundary}--\r\n".encode())
    return {"Content-Type": "multipart/form-data; boundary=" + boundary, "Content-Length": str(len(body))}, body
