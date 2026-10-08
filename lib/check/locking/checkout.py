import posixpath
import tempfile
from pathlib import Path

from lib.check import dependency
from lib.check.locking import literal, objects, qualify
from lib.content.static import workspace


def selected(inventory, selectors):
    if any("**" in selector.split("/") for selector in selectors):
        literal.refuse()
    result = {"package.json"}
    for name in inventory:
        if name == "package.json" or not name.endswith("/package.json"):
            continue
        directory = posixpath.dirname(name)
        if qualify.selected(directory, selectors):
            dependency.relative(name)
            if any(part.startswith(".") or part == "node_modules" for part in directory.split("/")):
                literal.refuse()
            result.add(name)
    if len(result) > dependency.FILES:
        literal.refuse()
    return sorted(result)


def project(root, inventory, env):
    required = {"package.json", "pnpm-workspace.yaml", "pnpm-lock.yaml"}
    if not required.issubset(inventory):
        literal.refuse()
    workspace_bytes = objects.blob(root, inventory["pnpm-workspace.yaml"], env, dependency.LIMIT)
    names = selected(inventory, dependency.workspace(workspace_bytes))
    manifests = {}
    total = len(workspace_bytes)
    for name in names:
        body = objects.blob(root, inventory[name], env, dependency.LIMIT - total)
        total += len(body)
        manifests[name] = body
    lockfile_bytes = objects.blob(root, inventory["pnpm-lock.yaml"], env, literal.LIMIT)
    return qualify.qualify(workspace_bytes, manifests, lockfile_bytes)


def collect(root, expected):
    identity = {key: expected[key] for key in ("repository", "commit", "tree")} if isinstance(expected, dict) and set(expected) == {"repository", "commit", "tree"} else None
    before = workspace.acquired(root, identity)
    root = Path(before["root"])
    with tempfile.TemporaryDirectory(prefix="wharf-preview-dependencies-") as home:
        env = workspace.environment(home)
        body = objects.command(root, ["ls-tree", "--full-tree", "-r", "-z", identity["tree"]], env, objects.INVENTORY)
        inventory = objects.inventory(body)
        projection = project(root, inventory, env)
    if workspace.acquired(root, identity) != before:
        literal.refuse()
    return {"schema": "wharf.preview.dependencies/v1", "source": identity, **projection}
