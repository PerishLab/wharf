import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from lib import parameters
from lib.content import executables, resources
from lib.media import release
from lib.process import run
from lib.refusal import Refusal
from lib.store import mirror
from lib.store.directory import Directory

WINDOWS = "x86_64-pc-windows-msvc"
REPOSITORY = "PerishLab/demo"
UNPINNED = "0" * 40
MARKERS = ("v1.0.0", "v1.1.0")
STEPS = (
    ("install", "v1.0.0", "v1.0.0", "v1.0.0"),
    ("install", "v1.0.0", "v1.0.0", "v1.0.0"),
    ("update", "v1.1.0", "v1.1.0", "v1.1.0"),
    ("uninstall", "v1.1.0", "", "root"),
    ("install", release.CANONICAL, "v1.1.0", "v1.1.0"),
    ("uninstall", release.CANONICAL, "", ""),
)
INVOKED = ("-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File")


def compiled(scratch, name, marker):
    held = scratch / "build" / f"{name}-{marker}.exe"
    held.parent.mkdir(exist_ok=True)
    run(["rustc", "--edition", "2021", "-o", str(held), str(scratch / "fixture.rs")], scratch, dict(os.environ, FIXTURE_NAME=name, FIXTURE_VERSION=marker))
    output = scratch / marker / f"{name}-{WINDOWS}.exe"
    shutil.copyfile(held, output)
    output.chmod(0o755)


def declared(scratch):
    (scratch / "fixture.rs").write_bytes(resources.read_bytes("manager/fixture.rs"))
    source = scratch / "source"
    source.mkdir()
    (source / "plumb.toml").write_bytes(resources.read_bytes("manager/fixture.toml"))
    listed = executables.declared(source, release.targets(source))
    return executables.built(listed, WINDOWS), executables.installed(listed, WINDOWS)


def published(scratch, marker, listed):
    built, installed = listed
    bound = scratch / marker
    bound.mkdir()
    for name in built:
        compiled(scratch, name, marker)
    held = release.Release(REPOSITORY, marker, UNPINNED, UNPINNED)
    managers = scratch / "managers" / marker
    release.render(held, str(managers), {WINDOWS: installed})
    bucket = Directory(scratch / "authority")
    authority = release.place(held)[2]
    release.publish(held, release.Contents({WINDOWS: bound}, managers, {WINDOWS: installed}, {}), bucket, lambda url: bucket.get(url.removeprefix(f"{authority}/")))
    return managers / "manage.ps1"


def reported(path):
    return subprocess.run([str(path), "--version"], capture_output=True, text=True, check=True).stdout.strip()


def matching(seated, entry):
    return entry.is_file() and seated.read_bytes() == entry.read_bytes()


def observed(seat):
    files = sorted(path.relative_to(seat).as_posix() for path in seat.rglob("*") if path.is_file())
    versions = {name: reported(seat / name) for name in files if name.startswith("bin/")}
    copies = {name: matching(seat / name, seat / "bin" / Path(name).name) for name in files if name.endswith(".exe") and not name.startswith("bin/")}
    return {"files": files, "versions": versions, "copies": copies}


def expected(state, installed):
    if state in ("", "root"):
        return {"files": ["root/.demo-manager"] if state else [], "versions": {}, "copies": {}}
    seated = [f"root/{state}/{name}.exe" for name in installed]
    files = sorted([f"bin/{name}.exe" for name in installed] + ["root/.demo-manager", f"root/{state}/.demo-manager"] + seated)
    return {"files": files, "versions": {f"bin/{name}.exe": f"{name} {state}" for name in installed}, "copies": dict.fromkeys(seated, True)}


def invoked(shell, script, argv):
    done = subprocess.run([shell, *INVOKED, str(script), *argv], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    print(done.stdout, file=sys.stderr, end="")
    if done.returncode != 0:
        raise Refusal(f"{shell} {script.name} {' '.join(argv)} exited {done.returncode}")


def stepped(shell, managers, installed, url):
    seat = Path(tempfile.mkdtemp())
    held = []
    for command, manager, marker, state in STEPS:
        pinned = ["--version", marker] if marker else []
        invoked(shell, managers[manager], [command, "--public-url", url, *pinned, "--install-root", str(seat / "root"), "--bin-dir", str(seat / "bin")])
        found, wanted = observed(seat), expected(state, installed)
        if found != wanted:
            raise Refusal(f"after {command} through the {manager} manager: found {json.dumps(found)}, expected {json.dumps(wanted)}")
        held.append({"command": command, "manager": manager, "files": found["files"]})
    return held


def windows(held):
    if shutil.which(held["shell"]) is None:
        raise Refusal(f"{held['shell']} is not on this machine")
    scratch = Path(tempfile.mkdtemp())
    built, installed = declared(scratch)
    managers = {marker: published(scratch, marker, (built, installed)) for marker in MARKERS}
    managers[release.CANONICAL] = managers[MARKERS[-1]].parent / release.CANONICAL / "manage.ps1"
    authority = release.place(release.Release(REPOSITORY, "", "", ""))[2]
    with mirror.served(scratch / "authority", authority) as url:
        steps = stepped(held["shell"], managers, installed, url)
    return {"action": "manager.windows", "shell": held["shell"], "built": built, "installed": installed, "steps": steps}


ACTIONS = {"windows": (windows, ["shell"])}


def main(argv=None):
    given = sys.argv[1:] if argv is None else argv
    try:
        action, rest = parameters.acted("manager", ACTIONS, given)
        handler, names = ACTIONS[action]
        values, origins = parameters.resolve(action, names, rest)
        print("\n".join(parameters.report(values, origins)), file=sys.stderr)
        result = handler(values)
    except Refusal as refusal:
        print(f"manager: refused: {refusal}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
