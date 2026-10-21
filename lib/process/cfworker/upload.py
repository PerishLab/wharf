import base64
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import PurePosixPath

from lib.content import canonical, resources
from lib.content.lane import codec
from lib.content.static import assets, evidence
from lib.process.cfworker import apply, mapping, payload, reader
from lib.refusal import Refusal

MIME = payload.MIME


class Protocol(Refusal):
    clauses = ('response-status', 'response-origin', 'response-encoding', 'response-body',
               'envelope-type', 'envelope-success', 'envelope-errors-missing', 'envelope-errors',
               'result-shape', 'session-status', 'session-shape', 'completion-status',
               'completion-shape', 'intermediate-status', 'intermediate-shape',
               'deployment-identity', 'deployment-retained')

    def __init__(self, clause):
        if not isinstance(clause, str) or clause not in self.clauses:
            raise Refusal('cfworker diagnostic clause is unsupported')
        super().__init__('cfworker response validation refused')
        self.clause = clause


def diagnostic(stage, error, status=None):
    stages = ('session-response', 'session-shape', 'session-jwt', 'session-buckets', 'bucket-response', 'bucket-shape', 'bucket-jwt', 'deployment-response')
    if stage not in stages:
        raise Refusal('cfworker diagnostic stage is unsupported')
    if isinstance(error, urllib.error.HTTPError):
        category = 'http'
        status = error.code
    elif isinstance(error, TimeoutError):
        category = 'timeout'
    elif isinstance(error, Refusal):
        category = 'protocol'
    else:
        category = 'transport'
    status = status if type(status) is int and 100 <= status <= 599 else None
    clause = error.clause if isinstance(error, Protocol) and isinstance(error.clause, str) and error.clause in Protocol.clauses else None
    print(json.dumps({'schema': 'wharf.cfworker.diagnostic/v2', 'stage': stage, 'category': category, 'status': status, 'clause': clause}), file=sys.stderr, flush=True)


def jwt(value):
    if not isinstance(value, str) or len(value) > 8192 or not payload.COMPLETION.fullmatch(value):
        raise Refusal("cfworker upload requires a private provider JWT")
    try:
        encoded = value.split(".")[1]
        claims = evidence.decode(base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True))
    except (ValueError, Refusal):
        raise Refusal("cfworker upload JWT has unsupported private claims") from None
    if not isinstance(claims, dict) or ("wrangler_single_asset_uploads" in claims and claims["wrangler_single_asset_uploads"] is not False):
        raise Refusal("cfworker upload supports only explicit bulk custody; no mode fallback")
    return value


def buckets(value, manifest):
    if not isinstance(value, list) or len(value) > len(manifest):
        raise Refusal("cfworker upload bucket catalog exceeds its exact manifest")
    allowed = {item["hash"] for item in manifest.values()}
    seen, result = set(), []
    for group in value:
        if not isinstance(group, list) or not group or len(group) > len(allowed):
            raise Refusal("cfworker upload requires nonempty bounded buckets")
        for held in group:
            if not isinstance(held, str) or held not in allowed or held in seen:
                raise Refusal("cfworker upload bucket repeats or selects foreign content")
            seen.add(held)
        result.append(tuple(group))
    return tuple(result)


def multipart(group, files, request):
    manifest = payload.catalog(files)
    names = {entry["hash"]: name[1:] for name, entry in manifest.items()}
    boundary = "wharf-assets-" + request
    result = []
    for held in group:
        name = names[held]
        kind = MIME.get(PurePosixPath(name).suffix.lower())
        if kind is None:
            raise Refusal("cfworker upload asset has no fixed admitted media type")
        result.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{held}"; filename="{held}"\r\nContent-Type: {kind}\r\n\r\n'.encode() + base64.b64encode(files[name]) + b"\r\n")
    body = b"".join(result) + f"--{boundary}--\r\n".encode()
    if len(body) > assets.MAX_TOTAL * 2:
        raise Refusal("cfworker upload multipart exceeds its finite byte budget")
    return {"Content-Type": "multipart/form-data; boundary=" + boundary, "Content-Length": str(len(body))}, body


class Uploader:
    def __init__(self, token, plan):
        if not isinstance(token, str) or not token or any(not 33 <= ord(char) <= 126 for char in token):
            raise Refusal("cfworker upload needs a separately prepared asset-session credential")
        self._plan = apply.checked(plan)
        self._selected = mapping.selected(self._plan["registration"])
        self._request = codec.digest("request", self._plan["current"]["active"]["request"])
        self._token, self._upload, self._completion = token, None, None
        self._attempted, self._blocked = False, False
        self._status = None
        self._buckets, self._done = (), set()
        self._lock = threading.Lock()
        self._client = urllib.request.build_opener(urllib.request.ProxyHandler({}), reader.Redirect())

    def _post(self, suffix, material, credential):
        if suffix not in (f"/workers/scripts/{self._selected['parent']}/assets-upload-session", "/workers/assets/upload?base64=true"):
            raise Refusal("cfworker upload refuses any non-fixed transport endpoint")
        policy = resources.read_json("build.json")["cfworker"]
        url = f"{policy['api']}/accounts/{self._selected['account']}{suffix}"
        headers, body = material
        headers = dict(headers, Authorization="Bearer " + credential, Accept="application/json", **{"Accept-Encoding": "identity", "User-Agent": "wharf-lane"})
        request = urllib.request.Request(url, body, headers, method="POST")
        self._status = None
        with self._client.open(request, timeout=30) as response:
            self._status = response.status
            if response.status not in (200, 201):
                raise Protocol('response-status')
            if response.geturl() != url:
                raise Protocol('response-origin')
            if response.headers.get("Content-Encoding") not in (None, "identity"):
                raise Protocol('response-encoding')
            status, body = response.status, response.read(evidence.LIMIT + 1)
        try:
            reply = evidence.decode(body)
        except Refusal:
            raise Protocol('response-body') from None
        if not isinstance(reply, dict):
            raise Protocol('envelope-type')
        if reply.get("success") is not True:
            raise Protocol('envelope-success')
        if "errors" not in reply:
            raise Protocol('envelope-errors-missing')
        if reply["errors"] not in (None, []):
            raise Protocol('envelope-errors')
        value = reply.get("result")
        intermediate = suffix == "/workers/assets/upload?base64=true" and status == 200
        if not isinstance(value, dict) and not (intermediate and value is None):
            raise Protocol('result-shape')
        return status, value

    def result(self, acknowledged):
        return {"request": self._request, "acknowledged": acknowledged, "buckets": len(self._buckets), "complete": self._completion is not None and not self._blocked}

    def session(self, document, bodies):
        with self._lock:
            if self._attempted:
                raise Refusal("cfworker asset session was already attempted; no retry")
            manifest = payload.manifest(self._plan, document, bodies)
            body = canonical.encode({"manifest": manifest})
            material = ({"Content-Type": "application/json", "Content-Length": str(len(body))}, body)
            suffix = f"/workers/scripts/{self._selected['parent']}/assets-upload-session"
            self._attempted = True
            stage = 'session-response'
            try:
                status, reply = self._post(suffix, material, self._token)
                stage = 'session-shape'
                if status != 200:
                    raise Protocol('session-status')
                if set(reply) != {"jwt", "buckets"}:
                    raise Protocol('session-shape')
                stage = 'session-jwt'
                private = jwt(reply["jwt"])
                stage = 'session-buckets'
                groups = buckets(reply["buckets"], manifest)
            except (OSError, urllib.error.URLError, TimeoutError, Refusal) as error:
                self._blocked = True
                diagnostic(stage, error, self._status)
                return self.result(False)
            self._buckets, self._upload = groups, private
            if not groups:
                self._completion = private
            return self.result(True)

    def bucket(self, number, document, bodies):
        with self._lock:
            if self._blocked or self._upload is None or type(number) is not int or not 0 <= number < len(self._buckets) or number in self._done:
                raise Refusal("cfworker upload needs one fresh exact bucket; no unknown continuation or retry")
            _, files = apply.qualified(self._plan, document, bodies)
            material = multipart(self._buckets[number], files, self._request)
            self._done.add(number)
            stage = 'bucket-response'
            try:
                status, reply = self._post("/workers/assets/upload?base64=true", material, self._upload)
                stage = 'bucket-shape'
                final = len(self._done) == len(self._buckets)
                if status != (201 if final else 200):
                    raise Protocol('completion-status' if final else 'intermediate-status')
                if final and set(reply) != {"jwt"}:
                    raise Protocol('completion-shape')
                if not final and reply not in (None, {}):
                    raise Protocol('intermediate-shape')
                if final:
                    stage = 'bucket-jwt'
                    self._completion = jwt(reply["jwt"])
            except (OSError, urllib.error.URLError, TimeoutError, Refusal) as error:
                self._blocked = True
                diagnostic(stage, error, self._status)
                return self.result(False)
            return self.result(True)

    def completion(self, document, bodies):
        with self._lock:
            apply.qualified(self._plan, document, bodies)
            if self._blocked or self._completion is None:
                raise Refusal("cfworker upload has no verified private completion acknowledgement")
            return self._completion
