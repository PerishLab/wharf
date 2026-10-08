import hashlib
import hmac
import json
import os
import re
from pathlib import Path

from lib.content import canonical
from lib.refusal import Refusal

LIMIT = 25 * 1024 * 1024
COORDINATE = re.compile(r"PerishLab/[A-Za-z0-9._-]+")
OBJECT = re.compile(r"[0-9a-f]{40}")
PURPOSE = b"wharf.follow.dispatch/v1\x00"
SECRET = "WHARF_FOLLOW_WEBHOOK_SECRET"
WORKFLOW = "PerishLab/wharf/.github/workflows/follow.yml@refs/heads/main"


def decode(body):
    if len(body) > LIMIT:
        raise Refusal("follow event exceeds the GitHub webhook size limit")
    try:
        held = json.loads(body)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise Refusal("follow event is not valid JSON") from error
    if not isinstance(held, dict):
        raise Refusal("follow event must be an object")
    return held


def signature(body, secret, prefix=b""):
    if not isinstance(secret, str) or not secret:
        raise Refusal("follow webhook secret is required")
    return "sha256=" + hmac.new(secret.encode(), prefix + body, hashlib.sha256).hexdigest()


def authenticate(body, observed, secret, prefix=b""):
    if not isinstance(observed, str) or not hmac.compare_digest(observed, signature(body, secret, prefix)):
        raise Refusal("follow event signature disagrees")


def checked(held):
    fields = {"schema", "repository", "repository_id", "installation_id", "after", "delivery"}
    if not isinstance(held, dict) or set(held) != fields or held.get("schema") != "wharf.follow.push/v1":
        raise Refusal("follow push summary has an unknown shape")
    repository = held["repository"]
    if not isinstance(repository, str) or not COORDINATE.fullmatch(repository) or repository.split("/")[1] in {".", ".."}:
        raise Refusal("follow target must be one PerishLab repository")
    if not isinstance(held["after"], str) or not OBJECT.fullmatch(held["after"]) or held["after"] == "0" * 40:
        raise Refusal("follow push needs an exact main commit")
    for field in ("repository_id", "installation_id"):
        if type(held[field]) is not int or held[field] <= 0:
            raise Refusal("follow push needs provider repository and installation identities")
    if not isinstance(held["delivery"], str) or not re.fullmatch(r"[A-Za-z0-9-]{1,100}", held["delivery"]):
        raise Refusal("follow push needs a bounded delivery identity")
    return held


def webhook(body, headers, secret):
    if len(body) > LIMIT:
        raise Refusal("follow webhook exceeds its size limit")
    authenticate(body, headers.get("X-Hub-Signature-256"), secret)
    if headers.get("X-GitHub-Event") != "push":
        raise Refusal("follow receiver accepts only push events")
    held = decode(body)
    if held.get("ref") != "refs/heads/main" or held.get("deleted") is not False:
        return None
    repository = held.get("repository")
    installation = held.get("installation")
    if not isinstance(repository, dict) or not isinstance(installation, dict) or repository.get("default_branch") != "main":
        raise Refusal("follow push has no governed main identity")
    summary = checked({"schema": "wharf.follow.push/v1", "repository": repository.get("full_name"), "repository_id": repository.get("id"), "installation_id": installation.get("id"), "after": held.get("after"), "delivery": headers.get("X-GitHub-Delivery")})
    return {"repository": summary["repository"], "summary": summary, "signature": signature(canonical.encode(summary), secret, PURPOSE)}


def qualified(path, environ=os.environ):
    context = {"GITHUB_REPOSITORY": "PerishLab/wharf", "GITHUB_WORKFLOW_REF": WORKFLOW, "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "repository_dispatch", "RUNNER_ENVIRONMENT": "self-hosted", "RUNNER_OS": "Linux"}
    if any(environ.get(field) != value for field, value in context.items()):
        raise Refusal("follow requires the trusted main workflow on its persistent Linux runner")
    with Path(path).open("rb") as source:
        event = decode(source.read(LIMIT + 1))
    actor = environ.get("WHARF_FOLLOW_SENDER_ID", "")
    sender = event.get("sender")
    if not isinstance(sender, dict) or not actor.isdecimal() or str(sender.get("id")) != actor or event.get("action") != "follow-push":
        raise Refusal("follow dispatch came from another receiver identity")
    payload = event.get("client_payload")
    if not isinstance(payload, dict) or set(payload) != {"repository", "summary", "signature"}:
        raise Refusal("follow dispatch has an unknown shape")
    summary = checked(payload["summary"])
    authenticate(canonical.encode(summary), payload["signature"], environ.get(SECRET), PURPOSE)
    if payload["repository"] != summary["repository"]:
        raise Refusal("follow concurrency target differs from the signed repository")
    return summary
