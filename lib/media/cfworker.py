import json
import os
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from lib.media import node
from lib.process import git, run
from lib.refusal import Refusal

CONFIG = "wrangler.jsonc"
TOKEN = "CLOUDFLARE_API_TOKEN"
WIDENED = ["the whole repository tree, because the worker builds from the pnpm workspace"]


@dataclass(frozen=True)
class Deploy:
    source: Path
    output: Path


def workers(source):
    found = []
    for path in sorted(git(source, "ls-files", f"*/{CONFIG}").splitlines()):
        text = re.sub(r"^\s*//.*$", "", (Path(source) / path).read_text(), flags=re.M)
        declared = json.loads(text)
        directory = path.rsplit("/", 1)[0]
        if not declared.get("account_id"):
            raise Refusal(f"{path} must declare account_id")
        domains = [route["pattern"] for route in declared.get("routes", []) if route.get("custom_domain")]
        package = node.manifest(source, f"{directory}/package.json")["name"]
        found.append({"name": declared["name"], "directory": directory, "package": package, "domains": domains})
    return found


def basis(source, runner):
    return {
        "entry": {"kind": "cfworker-deploy", "runner": runner, "workers": workers(source)},
        "engines": node.declared(source),
        "tree": git(source, "rev-parse", "HEAD^{tree}"),
        "widened": WIDENED,
    }


def answer(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "wharf"}), timeout=30) as response:
        return response.status


def deploy(request, runner=run, probe=answer):
    request = Deploy(Path(request.source).resolve(), Path(request.output))
    if request.output.exists():
        raise Refusal(f"output {request.output} already exists")
    if not os.environ.get(TOKEN):
        raise Refusal(f"{TOKEN} is required to deploy workers")
    held = node.prepared(request.source, runner)
    listed = workers(request.source)
    if not listed:
        raise Refusal(f"the product declares no {CONFIG}")
    runner(["pnpm", "install", "--frozen-lockfile"], request.source)
    results = []
    for worker in listed:
        runner(["pnpm", "--filter", worker["package"], "build"], request.source)
        runner(["pnpm", "exec", "wrangler", "deploy"], request.source / worker["directory"])
        reached = {domain: probe(f"https://{domain}/") for domain in worker["domains"]}
        if any(status != 200 for status in reached.values()):
            raise Refusal(f"{worker['name']} answered {reached} after deploying")
        results.append({"name": worker["name"], "domains": reached})
    request.output.mkdir(parents=True)
    receipt = {"action": "ship.cfworker", "toolchain": held, "workers": results}
    (request.output / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return receipt
