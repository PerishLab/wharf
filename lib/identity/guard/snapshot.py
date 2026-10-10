import json
import os
import re
import hashlib
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from lib.cargo import toolchain
from lib.content import resources
from lib.content.static import evidence
from lib.media import release
from lib.refusal import Refusal

HEX = re.compile(r"[0-9a-f]{40}")
REGISTRIES = resources.read_json("registries.json")["npm"]
RELEASES = resources.read_json("releases.json")
READER = "WHARF_PACKAGES_TOKEN"


@dataclass(frozen=True)
class Request:
    source: Path
    identity: dict
    domain: dict
    expected: object = None
    destination: object = None
    recall: object = None


def object_id(value):
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise Refusal("release snapshot needs an exact Git object identity")
    return value


def environment(home, domain):
    env = evidence.clean(os.environ, home)
    for name in ("NPM_CONFIG_IGNORE_SCRIPTS", "NPM_CONFIG_IGNORE_PNPMFILE"):
        env.pop(name)
    env.update({
        "USERPROFILE": str(home), "PLUMB_HOME": str(Path(home) / ".plumb"),
        "RUSTUP_HOME": os.environ.get("RUSTUP_HOME", str(Path.home() / ".rustup")),
        "CARGO_HOME": os.environ.get("CARGO_HOME", str(Path.home() / ".cargo")),
        "RUSTUP_TOOLCHAIN": domain["rust.version"],
        "GIT_AUTHOR_NAME": "Wharf", "GIT_AUTHOR_EMAIL": "wharf@perish.uk",
        "GIT_COMMITTER_NAME": "Wharf", "GIT_COMMITTER_EMAIL": "wharf@perish.uk",
        "GIT_AUTHOR_DATE": "@0 +0000", "GIT_COMMITTER_DATE": "@0 +0000",
    })
    if os.environ.get(READER):
        reader = os.environ[READER]
        if any(character in reader for character in ("\r", "\n")):
            raise Refusal("release snapshot package reader must occupy one configuration line")
        config = Path(home) / "npmrc"
        config.write_text("\n".join(f"//{location.split('://', 1)[1].rstrip('/')}/:_authToken={reader}" for location in sorted(set(REGISTRIES.values()))) + "\n")
        config.chmod(0o600)
        env["NPM_CONFIG_USERCONFIG"] = str(config)
    return env


def execute(held, argv):
    return held["inspect"](argv, held["root"], held["env"])


def git(held, *args):
    return execute(held, [held["tools"]["git"]["path"], *args]).strip()


def clean(held):
    if git(held, "status", "--porcelain", "--untracked-files=no"):
        raise Refusal("release snapshot changed tracked content")


def rust(source, held, inspect):
    tools, env, path = held
    for name in ("cargo", "rustc", "rustup"):
        found = shutil.which(name, path=path)
        if found is None:
            raise Refusal(f"release snapshot requires prepared {name}")
        executable = Path(found).absolute()
        if executable.resolve().is_relative_to(source) or not executable.is_file():
            raise Refusal(f"release snapshot {name} must be outside product source")
        tools[name] = {"path": str(executable), "sha256": hashlib.sha256(executable.read_bytes()).hexdigest()}
    env["PATH"] = os.pathsep.join(dict.fromkeys([str(Path(tools[name]["path"]).parent) for name in ("cargo", "rustc", "rustup")] + env["PATH"].split(os.pathsep)))
    for name in ("cargo", "rustc", "rustup"):
        tools[name]["version"] = inspect([tools[name]["path"], "--version"], source, env).strip()


def system(held):
    if os.name == "posix":
        held["env"]["PATH"] = os.pathsep.join(dict.fromkeys(held["env"]["PATH"].split(os.pathsep) + ["/usr/sbin", "/sbin"]))


def controls(held):
    tools = held["tools"]
    versions = toolchain.checked(evidence.decode(execute(held, [tools["plumb"]["path"], "metadata", "--json"])))
    if versions != held["request"].domain:
        raise Refusal("release snapshot domain changed; prepare a fresh plan")
    for name in ("node", "pnpm"):
        if name in tools and tools[name]["version"].lstrip("v") != versions[f"{name}.version"]:
            raise Refusal(f"release snapshot requires prepared domain {name}")
    authority = evidence.decode(execute(held, [tools["plumb"]["path"], "release", "authority", "--json"]))
    if not isinstance(authority, dict) or set(authority) != {"producer", "depot"} or not isinstance(authority["producer"], str) or not isinstance(authority["depot"], str) or not re.fullmatch(r"[0-9a-f]{64}", authority["depot"]):
        raise Refusal("release snapshot needs the exact running Plumb authority")
    version, separator, commit = authority["producer"].partition("@")
    if separator != "@" or version != tools["plumb"]["version"].removeprefix("plumb "):
        raise Refusal("release snapshot Plumb version and authority disagree")
    object_id(commit)
    held["authority"] = authority
    return dict({name: tools[name]["version"] for name in ("plumb", "ectropy")}, authority=authority, stable=stable(held))


def stable(held, reader=release.fetch):
    observed = {}
    for name in ("plumb", "ectropy"):
        authority = RELEASES["authority"].format(name=name)
        try:
            pointer = evidence.decode(reader(f"{authority}/v1/channels/stable.json"))
            if not isinstance(pointer, dict) or pointer.get("schema") != 1 or pointer.get("product") != name or pointer.get("channel") != "stable":
                raise Refusal("release snapshot has no current stable control pointer")
            if pointer.get("releaseVersion") != held["tools"][name]["version"].removeprefix(name + " "):
                raise Refusal(f"latest stable {name} changed; prepare current tools and a fresh plan")
            object_id(pointer.get("commit"))
            location = f"{authority}/v1/releases/stable/{pointer['releaseVersion']}/seal.json"
            sealed = pointer.get("seal", {})
            if not isinstance(sealed, dict) or sealed.get("url") != location:
                raise Refusal("release snapshot stable seal is not canonical")
            body = reader(location)
            if len(body) != sealed.get("size") or hashlib.sha256(body).hexdigest() != sealed.get("sha256"):
                raise Refusal("release snapshot stable seal differs from its pointer")
            seal = evidence.decode(body)
            if not isinstance(seal, dict) or any(seal.get(field) != pointer[field] for field in ("product", "releaseVersion", "commit")):
                raise Refusal("release snapshot stable seal identity disagrees")
            if name == "plumb" and seal.get("guard") != held["authority"]:
                raise Refusal("running Plumb differs from its current stable seal authority")
            observed[name] = {field: pointer[field] for field in ("releaseVersion", "commit")}
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise Refusal("latest stable control authority is unreadable") from error
    return observed


def resolution(held):
    tools = held["tools"]
    result = evidence.decode(execute(held, [tools["plumb"]["path"], "lift", str(held["root"]), "--json"]))
    if not isinstance(result, dict) or set(result) != {"context", "tree", "packages"} or result["context"] != "lift":
        raise Refusal("release snapshot needs actual released lift evidence")
    object_id(result["tree"])
    packages = result["packages"]
    if not isinstance(packages, list):
        raise Refusal("release snapshot has no exact package set")
    for package in packages:
        if not isinstance(package, dict) or set(package) != {"ecosystem", "name", "version"} or not all(isinstance(value, str) and value for value in package.values()):
            raise Refusal("release snapshot package is malformed")
    if packages != sorted(packages, key=lambda item: (item["ecosystem"], item["name"], item["version"])) or len({(item["ecosystem"], item["name"]) for item in packages}) != len(packages):
        raise Refusal("release snapshot package set is ambiguous")
    git(held, "add", "--update")
    if git(held, "write-tree") != result["tree"]:
        raise Refusal("released lift evidence differs from its actual staged tree")
    changed = git(held, "diff", "--name-only", "--cached", "HEAD").splitlines()
    if any(Path(path).name not in {"Cargo.toml", "Cargo.lock", "package.json", "pnpm-lock.yaml"} for path in changed):
        raise Refusal("release snapshot changed an unregistered dependency path")
    return result


def committed(held, result):
    head = git(held, "commit-tree", result["tree"], "-m", "First-party release snapshot")
    object_id(head)
    git(held, "reset", "--hard", head)
    clean(held)
    return head


def proof(held, head, result, body):
    request = held["request"]
    intent = {"repository": request.identity["repository"], "source": {"commit": head, "tree": result["tree"]}}
    proof = evidence.guard(body, intent, held["tools"]["plumb"]["version"])
    if {"producer": proof["guard"]["plumb"], "depot": proof["guard"]["depot"]} != held["authority"]:
        raise Refusal("release snapshot Guard differs from its running authority")
    resolved = proof["guard"].get("resolution")
    if resolved != {"context": "ci-latest", "tree": result["tree"], "packages": result["packages"]}:
        raise Refusal("latest packages changed during full Guard; prepare and verify a fresh plan")
    return proof


def verified(held, head, result):
    env = dict(held["env"], PLUMB_GUARD_STRENGTH="full", PLUMB_GUARD_BOUNDARY="head")
    body = held["inspect"]([held["tools"]["plumb"]["path"], "guard", "--json", str(held["root"])], held["root"], env)
    record = proof(held, head, result, body)
    if Path(record["root"]).resolve() != held["root"]:
        raise Refusal("release snapshot Guard observed another checkout")
    return record


def receipt(held, head, result, world):
    request = held["request"]
    observed = {"schema": "wharf.release.snapshot/v1", "source": request.identity, "head": head, "tree": result["tree"], "packages": result["packages"], "controls": world, "domain": request.domain}
    if request.expected is not None:
        expected = request.expected
        if not isinstance(expected, dict) or {key: value for key, value in expected.items() if key != "guard"} != observed or not isinstance(expected.get("guard"), dict):
            raise Refusal("release snapshot differs from the verified plan; prepare a fresh plan")
        proof(held, head, result, json.dumps(expected["guard"]))
        return expected
    recalled = request.recall(observed) if request.recall is not None else None
    if recalled is not None:
        proof(held, head, result, json.dumps(recalled))
        return dict(observed, guard=recalled)
    return dict(observed, guard=verified(held, head, result))


def unchanged(held, head, tree):
    if git(held, "rev-parse", "HEAD") != head or git(held, "rev-parse", "HEAD^{tree}") != tree:
        raise Refusal("release snapshot identity changed during execution")
    clean(held)
    evidence.unchanged(held["tools"])


def destination(source):
    return Path(source).resolve().parent / resources.read_json("build.json")["snapshot"]


def remove_readonly(function, path, error):
    if not isinstance(error, PermissionError):
        raise error
    Path(path).chmod(0o700)
    function(path)


@contextmanager
def checkout(source, root):
    root = Path(root)
    if root != root.resolve() or root == source or root.is_relative_to(source) or source.is_relative_to(root) or root.exists():
        raise Refusal("release snapshot needs an absent canonical owned destination")
    root.mkdir()
    identity = root.stat()
    try:
        yield root
    finally:
        if not root.is_dir() or (root.stat().st_dev, root.stat().st_ino) != (identity.st_dev, identity.st_ino):
            raise Refusal("release snapshot destination ownership changed; preserve it for review")
        shutil.rmtree(root, onexc=remove_readonly)


@contextmanager
def prepared(request, inspect=evidence.inspect):
    source = Path(request.source).resolve()
    domain = toolchain.checked(request.domain)
    for key in ("commit", "tree"):
        object_id(request.identity.get(key))
    with tempfile.TemporaryDirectory(prefix="wharf-snapshot-") as temporary, checkout(source, request.destination or Path(temporary) / "product") as root:
        directory = Path(temporary)
        home = directory / "home"
        home.mkdir()
        env = environment(home, domain)
        path = env["PATH"]
        tools = evidence.tools(source, env, inspect, ("plumb", "ectropy", "git"))
        held = {"request": request, "root": directory, "env": env, "tools": tools, "inspect": inspect}
        git(held, "clone", "--no-hardlinks", "--no-checkout", "--local", str(source), str(root))
        held["root"] = root
        git(held, "checkout", "--detach", request.identity["commit"])
        git(held, "remote", "set-url", "origin", f"https://github.com/{request.identity['repository']}.git")
        if git(held, "rev-parse", "HEAD^{tree}") != request.identity["tree"]:
            raise Refusal("release marker checkout differs from the declared source")
        paths = git(held, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
        if any(Path(name).name in {"package.json", "pnpm-lock.yaml"} for name in paths):
            env["PATH"] = path
            tools = evidence.tools(source, env, inspect)
            held["tools"] = tools
        if "Cargo.toml" in paths:
            rust(source, (tools, env, path), inspect)
            if tools["rustc"]["version"].split()[1] != domain["rust.version"]:
                raise Refusal("release snapshot requires prepared domain Rust")
        system(held)
        clean(held)
        world = controls(held)
        execute(held, [tools["plumb"]["path"], "configuration", "install"])
        result = resolution(held)
        head = committed(held, result)
        record = receipt(held, head, result, world)
        confirm = lambda: unchanged(held, head, result["tree"])
        confirm()
        yield root, record, confirm
        confirm()
