import hashlib
import json
import re

from lib.content import resources
from lib.refusal import Refusal

LIMIT = 1024 * 1024
FILES = 128
FIELDS = {"dependencies", "devDependencies", "optionalDependencies", "peerDependencies"}
FORBIDDEN = {"engines", "packageManager", "devEngines", "pnpm", "configDependencies", "workspaces"}
REGISTRIES = resources.read_json("registries.json")["npm"]
SEGMENT = re.compile(r"[A-Za-z0-9_*-][A-Za-z0-9_.*-]*")
PACKAGE = re.compile(r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*")
VERSION = re.compile(r"(?:\^|~)?(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){0,2}(?:-[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*)?")


def refuse():
    raise Refusal("Preview dependency configuration is outside the supported literal subset")


def decode(body):
    if not isinstance(body, bytes) or len(body) > LIMIT:
        refuse()
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        refuse()


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            refuse()
        result[key] = value
    return result


def constant(value):
    refuse()


def relative(value, wildcard=False):
    if not isinstance(value, str) or not value or len(value) > 256:
        refuse()
    for part in value.split("/"):
        if part in {"", ".", ".."} or not SEGMENT.fullmatch(part):
            refuse()
        if not wildcard and "*" in part:
            refuse()
    return value


def workspace(body):
    lines = decode(body).splitlines()
    if lines == ["packages: []"]:
        return []
    if not lines or lines[0] != "packages:" or len(lines) < 2:
        refuse()
    selectors = []
    for line in lines[1:]:
        if not line.startswith("  - "):
            refuse()
        value = line[4:]
        if value.startswith("*"):
            refuse()
        if value.startswith(("'", '"')):
            if len(value) < 3 or value[-1] != value[0]:
                refuse()
            value = value[1:-1]
        selectors.append(relative(value, wildcard=True))
    if len(selectors) > FILES or len(set(selectors)) != len(selectors):
        refuse()
    return selectors


def requirement(value):
    if not isinstance(value, str) or len(value) > 128:
        refuse()
    if value.startswith("workspace:"):
        value = value.removeprefix("workspace:")
        if value in {"*", "^", "~"}:
            return
    if not VERSION.fullmatch(value):
        refuse()


def manifest(body):
    try:
        value = json.loads(decode(body), object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, RecursionError):
        refuse()
    if not isinstance(value, dict) or FORBIDDEN.intersection(value):
        refuse()
    for field in FIELDS.intersection(value):
        dependencies = value[field]
        if not isinstance(dependencies, dict):
            refuse()
        for name, version in dependencies.items():
            if len(name) > 214 or not PACKAGE.fullmatch(name):
                refuse()
            requirement(version)


def qualify(workspace_bytes, manifests):
    if not isinstance(manifests, dict) or not 1 <= len(manifests) <= FILES:
        refuse()
    if "package.json" not in manifests:
        refuse()
    decode(workspace_bytes)
    total = len(workspace_bytes)
    for name, body in manifests.items():
        relative(name)
        if name.split("/")[-1] != "package.json":
            refuse()
        decode(body)
        total += len(body)
    if total > LIMIT:
        refuse()
    workspace(workspace_bytes)
    for body in manifests.values():
        manifest(body)
    digests = {"pnpm-workspace.yaml": hashlib.sha256(workspace_bytes).hexdigest()}
    digests.update({name: hashlib.sha256(body).hexdigest() for name, body in sorted(manifests.items())})
    npmrc = "registry=https://registry.npmjs.org/\n"
    npmrc += "".join(f"{scope}:registry={url}\n" for scope, url in sorted(REGISTRIES.items()))
    return {"version": 1, "digests": digests, "files": {"pnpm-workspace.yaml": "packages: []\n", ".npmrc": npmrc}}
