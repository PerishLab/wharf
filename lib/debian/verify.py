import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from lib.content import resources
from lib.process import run, stream
from lib.refusal import Refusal

VERIFIER = resources.read_json("build.json")["verify"]["deb"]
PLACED = "/tmp"
QUIET = ("-e", "DEBIAN_FRONTEND=noninteractive")


@dataclass(frozen=True)
class Check:
    deb: Path
    binary: str
    version: str
    marker: str
    depends: tuple
    units: dict


@dataclass(frozen=True)
class Tools:
    run: object = run
    stream: object = stream


def called(tool, argv):
    try:
        return tool(argv, ".")
    except subprocess.CalledProcessError as failure:
        detail = (failure.stderr or failure.stdout or "").strip()[-500:]
        raise Refusal(f"{' '.join(argv)} exited {failure.returncode}: {detail}")


def inside(container, *argv):
    return ["docker", "exec", *QUIET, container, *argv]


def status(text, binary):
    held = dict(line.split(": ", 1) for line in text.splitlines() if ": " in line and not line.startswith(" "))
    if held.get("Package") != binary or held.get("Status") != "install ok installed":
        raise Refusal(f"dpkg -s {binary} reports {held.get('Status')!r}, not an installed package")
    return held["Version"]


def installed(check, container, tools):
    if check.depends:
        called(tools.stream, inside(container, "apt-get", "update"))
        called(tools.stream, inside(container, "apt-get", "install", "-y", "--no-install-recommends", *check.depends))
    called(tools.stream, inside(container, "dpkg", "-i", f"{PLACED}/{check.deb.name}"))
    version = status(called(tools.run, inside(container, "dpkg", "-s", check.binary)), check.binary)
    if version != check.version:
        raise Refusal(f"the installed package reports version {version}, the release is {check.version}")
    listed = set(called(tools.run, inside(container, "dpkg", "-L", check.binary)).splitlines())
    missing = sorted({f"/usr/bin/{check.binary}", *check.units} - listed)
    if missing:
        raise Refusal(f"the installed package does not list {', '.join(missing)}")
    return version


def matched(check, container, tools):
    if check.units:
        summed = called(tools.run, inside(container, "sha256sum", *sorted(check.units)))
        found = {path: digest for digest, path in (line.split(None, 1) for line in summed.splitlines())}
        drifted = sorted(path for path, digest in check.units.items() if found.get(path) != digest)
        if drifted:
            raise Refusal(f"the installed {', '.join(drifted)} differs from the product's source")
    reported = called(tools.run, inside(container, f"/usr/bin/{check.binary}", "--version")).strip()
    if reported != f"{check.binary} {check.marker}":
        raise Refusal(f"/usr/bin/{check.binary} --version reported {reported!r}, expected '{check.binary} {check.marker}'")
    return reported


def verify(check, output, tools=Tools()):
    output = Path(output)
    if output.exists():
        raise Refusal(f"output {output} already exists")
    if not Path(check.deb).is_file():
        raise Refusal(f"{check.deb} is missing")
    called(tools.stream, ["docker", "pull", "--platform", VERIFIER["platform"], VERIFIER["image"]])
    container = called(tools.run, ["docker", "run", "-d", "--platform", VERIFIER["platform"], VERIFIER["image"], "sleep", "infinity"]).strip()
    try:
        called(tools.run, ["docker", "cp", str(check.deb), f"{container}:{PLACED}/{check.deb.name}"])
        version = installed(check, container, tools)
        reported = matched(check, container, tools)
    finally:
        tools.run(["docker", "rm", "-f", container], ".")
    output.mkdir(parents=True)
    receipt = {"action": "ship.deb.verify", "image": VERIFIER["image"], "package": check.binary, "version": version, "depends": list(check.depends), "units": check.units, "reported": reported}
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return receipt
