import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from lib.content import declaration
from lib.media import node
from lib.process import git, run
from lib.refusal import Refusal

CONFIG = "wrangler.jsonc"
TOKEN = "CLOUDFLARE_API_TOKEN"
WIDENED = ["the whole repository tree, because the worker builds from the pnpm workspace"]
PATIENCE = 40
PAUSE = 15


@dataclass(frozen=True)
class Deploy:
    source: Path
    output: Path


def workers(source):
    previews = declaration.previews(source)
    paths = sorted(git(source, "ls-files", f"*/{CONFIG}").splitlines())
    missing = {f"{directory}/{CONFIG}" for directory in previews} - set(paths)
    if missing:
        raise Refusal(f"preview apps must have tracked worker configurations: {sorted(missing)}")
    found = []
    targets = {}
    for path in paths:
        if (Path(source) / path).is_symlink():
            raise Refusal(f"{path} must not be a symlink")
        text = re.sub(r"^\s*//.*$", "", (Path(source) / path).read_text(), flags=re.M)
        declared = json.loads(text)
        directory = path.rsplit("/", 1)[0]
        if not isinstance(declared, dict) or any(not isinstance(declared.get(key), str) or not declared[key] for key in ("account_id", "name")):
            raise Refusal(f"{path} must declare account_id and name")
        target = (declared["account_id"], declared["name"])
        previous = targets.get(target)
        if previous is not None and (directory in previews or previous in previews):
            raise Refusal(f"preview worker target {target} is shared by {previous} and {directory}")
        targets[target] = directory
        package = node.manifest(source, f"{directory}/package.json")["name"]
        if directory in previews:
            if package != previews[directory]["package"]:
                raise Refusal(f"preview app {directory} package does not match its declaration")
            continue
        domains = [route["pattern"] for route in declared.get("routes", []) if route.get("custom_domain")]
        found.append({"name": declared["name"], "directory": directory, "package": package, "domains": domains})
    return found


def basis(source, runner):
    return {
        "entry": {"kind": "cfworker-deploy", "runner": runner, "workers": workers(source)},
        "engines": node.expected(source),
        "tree": git(source, "rev-parse", "HEAD^{tree}"),
        "widened": WIDENED,
    }


def answer(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "wharf"}), timeout=30) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def reached(url, probe, sleep):
    for attempt in range(PATIENCE):
        try:
            return probe(url)
        except OSError as error:
            last = error
            if attempt + 1 < PATIENCE:
                sleep(PAUSE)
    raise Refusal(f"{url} stayed unreachable for {PATIENCE} attempts after deploying: {last}")


def deploy(request, runner=run, probe=answer, sleep=time.sleep):
    request = Deploy(Path(request.source).resolve(), Path(request.output))
    if request.output.exists():
        raise Refusal(f"output {request.output} already exists")
    if not os.environ.get(TOKEN):
        raise Refusal(f"{TOKEN} is required to deploy workers")
    listed = workers(request.source)
    if not listed:
        raise Refusal(f"the product declares no {CONFIG}")
    held = node.prepared(request.source, runner)
    with tempfile.TemporaryDirectory() as directory:
        runner(["pnpm", "install", "--frozen-lockfile"], request.source, node.reading(directory, dict(os.environ)))
    results = []
    for worker in listed:
        runner(["pnpm", "--filter", worker["package"], "build"], request.source)
        runner(["pnpm", "exec", "wrangler", "deploy"], request.source / worker["directory"])
        answered = {domain: reached(f"https://{domain}/", probe, sleep) for domain in worker["domains"]}
        if any(status != 200 for status in answered.values()):
            raise Refusal(f"{worker['name']} answered {answered} after deploying")
        results.append({"name": worker["name"], "domains": answered})
    request.output.mkdir(parents=True)
    receipt = {"action": "ship.cfworker", "toolchain": held, "workers": results}
    (request.output / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return receipt
