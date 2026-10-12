import json
import os
import sys
import urllib.error
import urllib.request

from lib.content import canonical
from lib.process.follow import deadline, event
from lib.refusal import Refusal

ENDPOINT = "https://api.github.com/repos/PerishLab/wharf/dispatches"
TOKEN = "WHARF_FOLLOW_DISPATCH_TOKEN"
TIMEOUT = 5


def record(state, identity):
    print(json.dumps(dict(identity, state=state), sort_keys=True), file=sys.stderr, flush=True)


def arrival(environ):
    value = environ.get("HTTP_X_GITHUB_DELIVERY", "")
    valid = isinstance(value, str) and 0 < len(value) <= 100 and value.isascii() and all(char.isalnum() or char == "-" for char in value)
    record("received-unverified", {"delivery": value if valid else None})


def dispatch(payload, environ=os.environ):
    token = environ.get(TOKEN)
    if not token:
        raise deadline.Unverified("follow receiver needs its Wharf-only dispatch credential")
    body = canonical.encode({"event_type": "follow-push", "client_payload": payload})
    request = urllib.request.Request(ENDPOINT, data=body, method="POST", headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "Content-Type": "application/json", "User-Agent": "wharf-follow"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            if response.status != 204:
                raise deadline.Unverified("GitHub did not confirm acceptance of the follow dispatch")
    except (OSError, urllib.error.HTTPError) as error:
        raise deadline.Unverified("GitHub follow dispatch acceptance is unknown; read back before bounded recovery") from error


def receive(body, headers, environ=os.environ, send=dispatch):
    if len(body) > event.LIMIT:
        raise Refusal("follow webhook exceeds its size limit")
    secret = environ.get(event.SECRET)
    if headers.get("X-GitHub-Event") == "ping":
        event.authenticate(body, headers.get("X-Hub-Signature-256"), secret)
        return {"state": "ignored"}
    payload = event.webhook(body, headers, secret)
    if payload is None:
        return {"state": "ignored"}
    record("dispatching", payload["summary"])
    try:
        send(payload, environ)
    except (Refusal, OSError):
        record("unverified", payload["summary"])
        raise
    record("dispatched", payload["summary"])
    return {"state": "dispatched", "repository": payload["repository"], "delivery": payload["summary"]["delivery"]}


def application(environ, start_response):
    status = "202 Accepted"
    try:
        with deadline.limit():
            arrival(environ)
            length = environ.get("CONTENT_LENGTH", "")
            if environ.get("REQUEST_METHOD") != "POST" or not length.isdecimal() or not 0 < int(length) <= event.LIMIT:
                raise Refusal("follow receiver requires a bounded POST")
            body = environ["wsgi.input"].read(int(length))
            if len(body) != int(length):
                raise Refusal("follow webhook body is incomplete")
            headers = {name: environ.get(key) for name, key in (("X-Hub-Signature-256", "HTTP_X_HUB_SIGNATURE_256"), ("X-GitHub-Event", "HTTP_X_GITHUB_EVENT"), ("X-GitHub-Delivery", "HTTP_X_GITHUB_DELIVERY"))}
            result = receive(body, headers)
    except deadline.Unverified as error:
        status, result = "503 Service Unavailable", {"state": "unverified", "reason": str(error)}
        record("unverified", {"phase": "request"})
    except (Refusal, KeyError, OSError) as error:
        status, result = "400 Bad Request", {"state": "refused", "reason": str(error)}
        record("refused", {"phase": "request"})
    response = json.dumps(result, sort_keys=True).encode()
    start_response(status, [("Content-Type", "application/json"), ("Content-Length", str(len(response))), ("Cache-Control", "no-store")])
    return [response]
