import json
import os
import urllib.error
import urllib.request

from lib.content import canonical
from lib.process.follow import event
from lib.refusal import Refusal

ENDPOINT = "https://api.github.com/repos/PerishLab/wharf/dispatches"
TOKEN = "WHARF_FOLLOW_DISPATCH_TOKEN"


def dispatch(payload, environ=os.environ):
    token = environ.get(TOKEN)
    if not token:
        raise Refusal("follow receiver needs its Wharf-only dispatch credential")
    body = canonical.encode({"event_type": "follow-push", "client_payload": payload})
    request = urllib.request.Request(ENDPOINT, data=body, method="POST", headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "Content-Type": "application/json", "User-Agent": "wharf-follow"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status != 204:
                raise Refusal("GitHub did not accept the follow dispatch")
    except (OSError, urllib.error.HTTPError) as error:
        raise Refusal("GitHub follow dispatch failed; retain the webhook delivery for bounded recovery") from error


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
    send(payload, environ)
    return {"state": "dispatched", "repository": payload["repository"], "delivery": payload["summary"]["delivery"]}


def application(environ, start_response):
    status = "202 Accepted"
    try:
        length = environ.get("CONTENT_LENGTH", "")
        if environ.get("REQUEST_METHOD") != "POST" or not length.isdecimal() or not 0 < int(length) <= event.LIMIT:
            raise Refusal("follow receiver requires a bounded POST")
        body = environ["wsgi.input"].read(int(length))
        if len(body) != int(length):
            raise Refusal("follow webhook body is incomplete")
        headers = {name: environ.get(key) for name, key in (("X-Hub-Signature-256", "HTTP_X_HUB_SIGNATURE_256"), ("X-GitHub-Event", "HTTP_X_GITHUB_EVENT"), ("X-GitHub-Delivery", "HTTP_X_GITHUB_DELIVERY"))}
        result = receive(body, headers)
    except (Refusal, KeyError, OSError) as error:
        status, result = "400 Bad Request", {"state": "refused", "reason": str(error)}
    response = json.dumps(result, sort_keys=True).encode()
    start_response(status, [("Content-Type", "application/json"), ("Content-Length", str(len(response))), ("Cache-Control", "no-store")])
    return [response]
