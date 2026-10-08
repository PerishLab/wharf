import fnmatch
import json
import os
import posixpath
import re
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from lib.content import declaration, resources
from lib.media.node import expected, manifest, reading
from lib.process import git, run
from lib.refusal import Refusal

KNOWN = resources.read_json("registries.json")["npm"]
TOKEN = "NODE_AUTH_TOKEN"
WORKSPACE = "pnpm-workspace.yaml"
UNVERSIONED = "0.0.0"


@dataclass(frozen=True)
class Tools:
    run: object = run
    reader: object = None


def declared(source):
    return declaration.release(source).get("npm")


def carried(source):
    return declared(source) is not None


def basis(source, marker):
    return {"entry": {"kind": "npm-pack", "marker": marker, "packages": publishable(source)}, "tree": git(source, "rev-parse", "HEAD^{tree}"), "engines": expected(source)}


def globs(source):
    if git(source, "ls-tree", "--name-only", "HEAD", "--", WORKSPACE) != WORKSPACE:
        raise Refusal(f"{WORKSPACE} is not tracked at HEAD")
    lines = (Path(source) / WORKSPACE).read_text().splitlines()
    if "packages:" not in lines:
        raise Refusal(f"{WORKSPACE} must list its packages in block style")
    found = []
    for line in lines[lines.index("packages:") + 1:]:
        if not line.strip():
            continue
        item = re.fullmatch(r"\s+-\s+[\"']?([^\"'#\s]+)[\"']?\s*", line)
        if not item:
            break
        found.append(item.group(1))
    if not found:
        raise Refusal(f"{WORKSPACE} lists no packages")
    return found


def publishable(source):
    attachment = declared(source)
    if attachment is None:
        return []
    names = attachment.get("packages", [])
    found = {}
    for path in workspace(source):
        held = manifest(source, path)
        if held.get("name") not in names:
            continue
        if held.get("private"):
            raise Refusal(f"{held['name']} is declared in [release.npm] and {path} marks it private")
        if held.get("version") != UNVERSIONED:
            raise Refusal(f"{path} must declare version {UNVERSIONED}")
        location = registry(source, held["name"])
        if location != attachment.get("registry"):
            raise Refusal(f"{held['name']} maps to {location!r}, [release.npm] declares {attachment.get('registry')!r}")
        found[held["name"]] = {"name": held["name"], "path": path, "registry": location}
    missing = [name for name in names if name not in found]
    if missing:
        raise Refusal(f"[release.npm] declares {', '.join(missing)}, which no workspace package names")
    return [found[name] for name in names]


def workspace(source):
    patterns = globs(source)
    listed = git(source, "ls-files", "*/package.json").splitlines()
    return [path for path in sorted(listed) if any(fnmatch.fnmatchcase(posix_parent(path), pattern) for pattern in patterns)]


def posix_parent(path):
    return path.rsplit("/", 1)[0]


def registry(source, name):
    scope = name.split("/", 1)[0] if name.startswith("@") else ""
    npmrc = Path(source) / ".npmrc"
    mapped = dict(re.findall(r"^(@[\w.-]+):registry=(\S+)\s*$", npmrc.read_text(), re.M)) if npmrc.is_file() else {}
    location = mapped.get(scope, "")
    if not scope or KNOWN.get(scope) != location:
        raise Refusal(f"npm package {name!r} maps to {location!r}, not a known distribution registry")
    return location


def fetch(url):
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {os.environ.get(TOKEN, '')}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as failure:
        if failure.code == 404:
            return {}
        raise Refusal(f"npm registry {url} answered {failure.code}")


def published(package, version, reader=fetch):
    url = package["registry"] + urllib.parse.quote(package["name"], safe="@")
    return version in reader(url).get("versions", {})


def pending(source, version, reader=fetch):
    return [dict(package, published=published(package, version, reader)) for package in publishable(source)]


def channel(version):
    matched = re.fullmatch(r"\d+\.\d+\.\d+(?:-(alpha|beta|rc)\.\d+)?", version)
    if not matched:
        raise Refusal(f"version {version!r} is not a release version")
    return matched.group(1) or "latest"


def stamp(source, package, version):
    path = Path(source) / package["path"]
    declared = json.loads(path.read_text())
    declared["version"] = version
    path.write_text(json.dumps(declared, indent="\t") + "\n")


def userconfig(directory, registries):
    if not os.environ.get(TOKEN):
        raise Refusal(f"{TOKEN} is required to publish npm packages")
    path = Path(directory) / "npmrc"
    lines = [f"//{urllib.parse.urlsplit(location).netloc}/:_authToken=${{{TOKEN}}}" for location in sorted(registries)]
    path.write_text("\n".join(lines) + "\n")
    return path


def prepare(source, version, output, runner=run):
    source, output = Path(source), Path(output)
    if output.exists():
        raise Refusal("npm archive output must be absent")
    listed = publishable(source)
    originals = {source / item["path"]: (source / item["path"]).read_bytes() for item in listed}
    env = dict(os.environ)
    env.pop(TOKEN, None)
    with tempfile.TemporaryDirectory() as directory:
        runner(["pnpm", "install", "--frozen-lockfile"], source, reading(directory, env))
        env.pop("WHARF_PACKAGES_TOKEN", None)
        output.mkdir(parents=True)
        try:
            for item in listed:
                stamp(source, item, version)
            stamped = {path: path.read_bytes() for path in originals}
            archives = []
            for item in listed:
                archive = pack(source, item, directory, lambda argv, cwd: runner(argv, cwd, env))
                problems = vet(archive)
                if problems:
                    raise Refusal("packed packages do not hold what they name:\n" + "\n".join(problems))
                name = item["name"].replace("/", "__") + ".tgz"
                (output / name).write_bytes(archive.read_bytes())
                archives.append({"name": item["name"], "registry": item["registry"], "file": name})
            if any(path.read_bytes() != expected for path, expected in stamped.items()):
                raise Refusal("npm packing changed its stamped manifest")
        finally:
            for path, original in originals.items():
                path.write_bytes(original)
    receipt = {"version": version, "packages": archives}
    (output / "receipt.json").write_text(json.dumps(receipt, sort_keys=True) + "\n")
    return receipt


def publish(source, version, directory, tools=Tools()):
    listed = publishable(source)
    receipt = json.loads((Path(directory) / "receipt.json").read_text())
    expected = [{"name": item["name"], "registry": item["registry"], "file": item["name"].replace("/", "__") + ".tgz"} for item in listed]
    if receipt != {"version": version, "packages": expected}:
        raise Refusal("npm archives differ from the declared release")
    archives = {item["name"]: Path(directory) / item["file"] for item in expected}
    for name, archive in archives.items():
        with tarfile.open(archive) as held:
            declared = json.load(held.extractfile("package/package.json"))
        if (declared.get("name"), declared.get("version")) != (name, version) or vet(archive):
            raise Refusal("npm archive content differs from the declared release")
    results = []
    with tempfile.TemporaryDirectory() as home:
        env = dict(os.environ, NPM_CONFIG_USERCONFIG=str(userconfig(home, {item["registry"] for item in listed})))
        for item in pending(source, version, tools.reader or fetch):
            if item["published"]:
                results.append({"name": item["name"], "state": "already-published"})
                continue
            tools.run(["pnpm", "publish", str(archives[item["name"]]), "--ignore-scripts", "--no-git-checks", "--registry", item["registry"], "--tag", channel(version)], source, env)
            if not published(item, version, tools.reader or fetch):
                raise Refusal(f"{item['name']}@{version} is not visible after publishing")
            results.append({"name": item["name"], "state": "published", "tag": channel(version)})
    return {"version": version, "packages": results}


SCRIPTS = (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".svelte")
RELATIVE = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(?\s*)["'](\.{1,2}/[^"']+)["']""")
ENTRIES = ("exports", "main", "module", "types", "typings", "svelte", "bin")


def pack(source, package, directory, runner):
    held = Path(directory) / "packed" / package["name"].replace("/", "__")
    held.mkdir(parents=True)
    runner(["pnpm", "pack", "--pack-destination", str(held)], Path(source) / posix_parent(package["path"]))
    found = sorted(held.glob("*.tgz"))
    if len(found) != 1:
        raise Refusal(f"packing {package['name']} left {len(found)} tarballs")
    return found[0]


def vet(tarball):
    with tarfile.open(tarball) as held:
        files = {member.name.split("/", 1)[1]: member for member in held.getmembers() if member.isfile() and "/" in member.name}
        read = lambda name: held.extractfile(files[name]).read().decode("utf-8", "replace")
        declared = json.loads(read("package.json"))
        name = declared.get("name", tarball.name)
        problems = [f"{name}: {field} names {target}, which the package does not carry" for field, target in targets(declared) if not carries(files, target)]
        imports = [(path, specifier) for path in sorted(files) if path.endswith(SCRIPTS) for specifier in RELATIVE.findall(read(path))]
    return problems + [f"{name}: {path} imports {specifier}, which the package does not carry" for path, specifier in imports if not resolves(files, path, specifier)]


def targets(declared):
    found = []
    def walk(field, value):
        if isinstance(value, str):
            if value.startswith("./") or field in ("main", "module", "types", "typings", "svelte", "bin"):
                found.append((field, value))
        elif isinstance(value, dict):
            for held in value.values():
                walk(field, held)
        elif isinstance(value, list):
            for held in value:
                walk(field, held)
    for field in ENTRIES:
        if field in declared:
            walk(field, declared[field])
    return found


def carries(files, target):
    path = posixpath.normpath(target)
    if "*" in path:
        prefix = path.split("*", 1)[0]
        return any(name.startswith(prefix) for name in files)
    return path in files or any(path + suffix in files for suffix in (".js", "/index.js"))


def resolves(files, importer, specifier):
    path = posixpath.normpath(posixpath.join(posixpath.dirname(importer), specifier))
    candidates = [path, *(path + suffix for suffix in (".js", ".ts", ".svelte", "/index.js", "/index.ts"))]
    if importer.endswith(".d.ts") and path.endswith(".js"):
        candidates.append(path[:-3] + ".d.ts")
    return any(candidate in files for candidate in candidates)
