import os
import re
import datetime
import http.client
from urllib.parse import quote

from lib.content import canonical, resources
from lib.content.lane import codec, preview, shape
from lib.content.static import admission, declaration, evidence
from lib.content.static.github import GitHub, LOGIN, Routing
from lib.refusal import Refusal
from lib.store import r2
from lib.store.lane import documents, registration, state
from lib.store.sigv4 import Credentials, Request, sign

FIELDS = {"schema", "target", "operation", "request", "revision", "expected", "source", "registration", "caller", "workflow"}


def requested(body):
    if not isinstance(body, str):
        raise Refusal("lane dispatch requires a bounded JSON string")
    try:
        encoded = body.encode()
    except UnicodeError:
        raise Refusal("lane dispatch requires valid UTF-8") from None
    if len(encoded) > 65536:
        raise Refusal("lane dispatch exceeds its document budget")
    value = evidence.decode(encoded)
    preview.shape(value, FIELDS, "lane dispatch")
    if value["schema"] != "wharf.lane.dispatch/v1" or value["operation"] not in ("apply", "inspect", "dispose"):
        raise Refusal("lane dispatch schema or operation is unsupported")
    value["target"] = codec.read("address", value["target"])
    preview.matches(value["target"]["repository"], re.compile(r"PerishLab/[A-Za-z0-9_-]+"), "lane repository")
    declaration.name(value["target"]["lane"])
    declaration.repository(value["target"]["repository"])
    preview.slug(value["target"]["app"], "lane app")
    for key in ("request", "registration"):
        value[key] = codec.read("digest", value[key])
    value["revision"] = shape.revision(value["revision"])
    value["expected"] = codec.read("state", value["expected"])
    if value["expected"]["state"] == "unknown":
        raise Refusal("lane dispatch needs an exact settled precondition")
    preview.matches(value["caller"], LOGIN, "lane caller")
    preview.text(value["workflow"], "lane control workflow")
    if value["operation"] == "apply":
        preview.source(value["source"])
    elif value["source"] is not None:
        raise Refusal("lane inspect/dispose require no product source")
    return documents.decode(documents.encode(value))


def policy():
    held = resources.read_json("build.json")
    return dict(held["admission"], workflow=held["lane"]["workflow"])


def platform(environ=os.environ):
    return admission.context(environ, policy())


def admit(intent, bucket, context, api):
    intent = requested(documents.encode(intent).decode())
    registered, _ = registration.read(bucket, intent["target"])
    if not registered["enabled"] or registration.digest(registered) != intent["registration"]:
        raise Refusal("lane dispatch registration is disabled or moved")
    current, _ = state.read(bucket, registered)
    if current["active"] is not None or current["revision"] != intent["revision"] or current["observed"] != intent["expected"]:
        raise Refusal("lane dispatch is busy or its exact state precondition moved")
    capability = "deploy" if intent["operation"] == "apply" else intent["operation"]
    if capability not in registered["declaration"]["capabilities"]:
        raise Refusal("lane dispatch capability is not registered")
    if intent["source"] is not None and intent["source"]["declaration"] != registered["generation"]:
        raise Refusal("lane source configuration generation differs")
    return dict(authority(intent, context, api), schema="wharf.lane.admission/v1", intent=canonical.digest(intent),
                registration=registered, current=current)


def authority(intent, context, api):
    intent = requested(documents.encode(intent).decode())
    run, control = admission.workflow(api, context, policy())
    repository = intent["target"]["repository"]
    actors = admission.actors(api, run, repository)
    expected = f"{control['repository']['name']}/{control['path']}@{control['commit']}"
    if intent["caller"] != actors["actor"]["login"] or intent["workflow"] != expected:
        raise Refusal("lane caller or control workflow differs from independent observations")
    product = admission.repository(api.repository(repository), repository)
    if intent["source"] is not None:
        origin = intent["source"]
        observed = api.commit(repository, origin["commit"])
        tree = observed.get("tree")
        if observed.get("sha") != origin["commit"] or not isinstance(tree, dict) or tree.get("sha") != origin["tree"]:
            raise Refusal("lane source commit/tree or configuration generation differs")
    return {"workflow": control, "actors": actors, "repository": product}


class Reader:
    connect = http.client.HTTPSConnection

    def __init__(self, intent, environ):
        intent = requested(documents.encode(intent).decode())
        names = ("ENDPOINT", "BUCKET", "ACCESS_KEY_ID", "SECRET_ACCESS_KEY")
        values = [r2.secret(environ, "WHARF_LANE_READER_" + name) for name in names]
        endpoint, bucket, access, secret = values
        preview.matches(endpoint, re.compile(r"https://[0-9a-f]{32}\.r2\.cloudflarestorage\.com"), "lane store origin")
        preview.matches(bucket, re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]"), "lane store bucket")
        if any(not value or any(not 33 <= ord(char) <= 126 for char in value) for value in (access, secret)):
            raise Refusal("lane read needs independently prepared credentials")
        self._host, self._bucket = endpoint[8:], bucket
        self._credentials = Credentials(access, secret)
        prefix = documents.location(intent["target"])
        self._keys = {prefix + "/registration.json", prefix + "/current.json"}

    def snapshot(self, key):
        if key not in self._keys:
            raise Refusal("lane read is outside its exact registration/current keys")
        path = "/" + self._bucket + "/" + quote(key, safe="/-_.~")
        headers = sign(Request("GET", self._host, path), self._credentials, datetime.datetime.now(datetime.UTC))
        connection = None
        try:
            connection = self.connect(self._host, timeout=30)
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            if response.status == 404:
                return None, None
            etag = response.getheader("ETag")
            if response.status != 200 or not isinstance(etag, str) or not etag or response.getheader("Content-Encoding") not in (None, "identity"):
                raise Refusal("lane current/registration read is not a versioned exact observation")
            body = response.read(65537)
            if not isinstance(body, bytes) or len(body) > 65536:
                raise Refusal("lane state read exceeds its document budget")
            return body, etag
        except (OSError, http.client.HTTPException):
            raise Refusal("lane state read failed; no authority inferred") from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except (OSError, http.client.HTTPException):
                    raise Refusal("lane read cleanup failed") from None


def observe(intent, environ=os.environ):
    context = platform(environ)
    reader = Reader(intent, environ)
    control = GitHub(r2.secret(environ, "WHARF_PREVIEW_CONTROL_TOKEN"))
    product = GitHub(r2.secret(environ, "WHARF_PREVIEW_PRODUCT_TOKEN"))
    return admit(intent, reader, context, Routing(intent["target"]["repository"], control, product))


def source(intent, observation):
    intent = requested(documents.encode(intent).decode())
    preview.shape(observation, {"schema", "intent", "registration", "current", "workflow", "actors", "repository"}, "lane admission")
    if observation["schema"] != "wharf.lane.admission/v1" or intent["operation"] != "apply" or observation["intent"] != canonical.digest(intent):
        raise Refusal("lane build requires exact admitted apply source")
    registered = registration.checked(observation["registration"])
    if registration.digest(registered) != intent["registration"] or registered["declaration"]["target"] != intent["target"]:
        raise Refusal("lane build admission changed registration or target")
    current = state.checked(observation["current"], registered)
    if current["active"] is not None or current["revision"] != intent["revision"] or current["observed"] != intent["expected"]:
        raise Refusal("lane build admission differs from its settled precondition")
    return {"schema": "wharf.lane.build-source/v1", "repository": intent["target"]["repository"],
            "app": intent["target"]["app"], "source": intent["source"], "registration": intent["registration"]}
