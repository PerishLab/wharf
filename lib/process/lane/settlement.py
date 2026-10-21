import http.client
import os

from lib.content.lane import codec
from lib.content.static import evidence
from lib.content.static.github import GitHub, Routing
from lib.process.lane import build, entry, execution, publication
from lib.refusal import Conflict, Refusal
from lib.store import r2
from lib.store.lane import documents, public, publication as kernel, registration, state


class Bucket(publication.Bucket):
    def permitted(self, operation):
        conditional = operation.headers.get("If-None-Match") == "*" or isinstance(operation.headers.get("If-Match"), str) and bool(operation.headers["If-Match"])
        if operation.query or operation.method not in ("GET", "PUT") or operation.method == "PUT" and not conditional:
            raise Refusal("lane state store accepts only exact reads and conditional writes")


def roles(environ):
    forbidden = tuple(name for name in build.FORBIDDEN if not name.startswith("WHARF_LANE_STATE_")) + (
        "WHARF_R2_ACCESS_KEY_ID", "WHARF_R2_SECRET_ACCESS_KEY", "WHARF_CLOUDFLARE_TOKEN",
        "WHARF_LANE_DEPLOYMENT_TOKEN", "WHARF_LANE_DISPOSAL_TOKEN", "WHARF_LANE_PROVIDER_READER_TOKEN",
        "WHARF_LANE_WORKLOAD_READER_ACCESS_KEY_ID", "WHARF_LANE_WORKLOAD_READER_SECRET_ACCESS_KEY")
    if any(r2.secret(environ, name) for name in forbidden):
        raise Refusal("lane state job holds only its state writer and required readers")
    if environ.get("GITHUB_JOB") != "state":
        raise Refusal("lane settlement requires its fixed original state job")


def published(before, reader, reference):
    prepared = before["execution"]["prepared"]
    operation = kernel.settle(reader, prepared["registration"]["declaration"], reference, prepared["request"])
    if any(operation[key] != prepared[key] for key in ("request", "assessment")):
        raise Refusal("lane public settlement differs from the entire original request/assessment")
    return operation


def confirm(before, environ):
    publication.confirm(before, environ)
    execution.quiescence(before["intent"], environ)


class Writer:
    def __init__(self, before, verified, environ):
        self.before, self.verified, self.environ = before, verified, environ
        prepared = before["execution"]["prepared"]
        self.prefix = documents.location(prepared["request"]["context"]["requested"])
        self.history = self.prefix + "/requests/" + prepared["request"]["context"]["request"] + "/" + codec.digest("operation", verified["operation"]) + ".json"
        shared = publication.configured(environ, "STATE")
        if r2.secret(environ, "WHARF_LANE_READER_ENDPOINT") != "https://" + shared.host or r2.secret(environ, "WHARF_LANE_READER_BUCKET") != shared.name:
            raise Refusal("lane state writer and original reservation reader mapping disagree")
        self.bucket = Bucket("https://" + shared.host, shared.name, shared.credentials)
        self.etag = None

    def snapshot(self, key):
        prepared = self.before["execution"]["prepared"]
        expected = {"registration": prepared["registration"], "current": prepared["current"]}
        selected = next((name for name in expected if key == self.prefix + "/" + name + ".json"), None)
        if selected is None:
            raise Refusal("lane state snapshot is outside its original registration/current")
        body, etag = documents.snapshot(self.bucket, key)
        if body is None or documents.decode(body) != expected[selected]:
            raise Refusal("lane state writer observes moved registration/current")
        if selected == "current":
            self.etag = etag
        return body, etag

    def get(self, key):
        if key != self.history:
            raise Refusal("lane state history read is outside its exact verified operation")
        return self.bucket.get(key)

    def authorize(self):
        confirm(self.before, self.environ)
        if published(self.before, self.verified["reader"], self.verified["reference"]) != self.verified["operation"]:
            raise Refusal("lane public applied record moved before conditional state write")
        confirm(self.before, self.environ)

    def create(self, key, body):
        if key != self.history or body != codec.encode("operation", self.verified["operation"]):
            raise Refusal("lane state history write differs from its exact verified operation")
        self.authorize()
        self.bucket.create(key, body)

    def swap(self, key, body, etag):
        prepared = self.before["execution"]["prepared"]
        proposed = state.finished(prepared["current"], self.verified["operation"], prepared["registration"])
        if key != self.prefix + "/current.json" or documents.decode(body) != proposed or not etag or etag != self.etag:
            raise Refusal("lane state CAS differs from its exact verified conditional transition")
        self.authorize()
        self.bucket.swap(key, body, etag)


def prepare(held, environ):
    roles(environ)
    intent = entry.requested(held["request"])
    observed = execution.observe(intent, evidence.decode(held["prepared"]), environ)
    before = {"intent": intent, "execution": observed}
    confirm(before, environ)
    reference = codec.read("reference", evidence.decode(held["reference"]))
    reader = public.prepare({"authority": r2.secret(environ, "WHARF_LANE_PUBLICATION_AUTHORITY"),
                             "origin": r2.secret(environ, "WHARF_LANE_PUBLICATION_ORIGIN")})
    operation = published(before, reader, reference)
    confirm(before, environ)
    return before, {"operation": operation, "reference": reference, "reader": reader}


def readback(before, proposed, writer):
    entry.authority(before["intent"], entry.platform(writer.environ), entry_api(before, writer.environ))
    execution.quiescence(before["intent"], writer.environ)
    registered, _ = registration.read(writer.bucket, before["intent"]["target"])
    actual, _ = state.read(writer.bucket, registered)
    prepared = before["execution"]["prepared"]
    if registered != prepared["registration"] or actual != proposed or writer.bucket.get(execution.location(before["intent"])) != documents.encode(prepared["custody"]):
        raise Refusal("lane state settlement has no exact independent current/custody readback")
    registration.confirm(writer.bucket, registered)
    return actual


def entry_api(before, environ):
    control = GitHub(r2.secret(environ, "WHARF_PREVIEW_CONTROL_TOKEN"))
    product = GitHub(r2.secret(environ, "WHARF_PREVIEW_PRODUCT_TOKEN"))
    return Routing(before["intent"]["target"]["repository"], control, product)


def perform(held, environ=os.environ):
    before, verified = prepare(held, environ)
    if before["intent"]["operation"] == "inspect":
        confirm(before, environ)
        return {"phase": "observed", "reference": verified["reference"]}
    writer = Writer(before, verified, environ)
    try:
        proposed = state.record(writer, verified["operation"])
        result = readback(before, proposed, writer)
    except Conflict:
        raise Refusal("lane state CAS or immutable history conflicted; no retry or takeover") from None
    except (OSError, http.client.HTTPException):
        raise Refusal("lane state settlement is unverified; no retry or inferred completion") from None
    return {"phase": result["phase"], "revision": result["revision"], "reference": verified["reference"]}
