import hashlib
import json
import subprocess
from pathlib import Path

from lib.media.release import fetch
from lib.process.trial.admission import checked, document
from lib.process.trial.declaration import validate, NAME

RUNNER = "https://releases.seatrial.perish.uk"
PROBE = "docker.io/curlimages/curl:8.10.1@sha256:d9b4541e214bcd85196d6e92e2753ac6d0ea699f0af5741f8c6cccbfcf00ef4b"


def verified(entry):
    checked(str(entry.get("url", "")).startswith(RUNNER + "/"), "runner material has a foreign authority")
    raw = fetch(entry["url"])
    checked(len(raw) == entry["size"] and hashlib.sha256(raw).hexdigest() == entry["sha256"], "runner material differs from its digest")
    return raw


def install(root, env):
    pointer = document(f"{RUNNER}/v1/channels/stable.json")
    checked(pointer.get("product") == "seatrial" and pointer.get("channel") == "stable", "runner channel identity differs")
    seal = json.loads(verified(pointer["seal"]))
    checked(all(seal.get(key) == pointer.get(key) for key in ("product", "channel", "releaseVersion", "commit")), "runner seal identity differs")
    manager = root / "manage.sh"
    manager.write_bytes(verified(pointer["managers"]["unix"]))
    command = ["sh", str(manager), "install", "--channel", "stable", "--install-root", str(root / "installed"), "--bin-dir", str(root / "bin")]
    with (root / "evidence" / "install.log").open("xb") as output:
        subprocess.run(command, env=env, stdout=output, stderr=subprocess.STDOUT, check=True)
    binary = root / "bin" / "seatrial"
    version = subprocess.check_output([str(binary), "--version"], env=env, text=True).strip()
    checked(version == f"seatrial {pointer['releaseVersion']}", "installed runner moved from observed stable")
    identity = {"pointer": pointer, "seal": seal, "version": version, "sha256": hashlib.sha256(binary.read_bytes()).hexdigest()}
    (root / "evidence" / "runner.json").write_text(json.dumps(identity, indent=2) + "\n")
    return binary


def execute(held):
    root = Path(held["source"]).resolve()
    admitted = json.loads((root / "evidence" / "admission.json").read_text())
    checked(admitted.get("eligible") is True, "observation is not admitted")
    current = document(f"{admitted['authority']}/v1/channels/{admitted['channel']}.json")
    if current.get("releaseVersion") != admitted["marker"]:
        result = {"state": "superseded", "ok": False, "requested": admitted["marker"], "current": current.get("releaseVersion")}
        (root / "evidence" / "execution.json").write_text(json.dumps(result, indent=2) + "\n")
        return result
    work = root / "work"
    work.mkdir()
    home = root / "home"
    home.mkdir()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "LANG": "C.UTF-8"}
    binary = install(root, env)
    specification = validate(admitted["trial"]["specification"])
    mode = held["trialcase"]
    checked(mode == "declared" or NAME.fullmatch(mode), "invalid diagnostic case")
    cases = specification["cases"] if mode == "declared" else [mode]
    configuration = {"images": {"probe": PROBE}, admitted["product"]: {"channel": admitted["channel"], "authority": admitted["authority"], "image": specification["image"]}, "scenes": specification["scenes"]}
    config = "".join("[" + name + "]\n" + "".join(key + " = " + json.dumps(value) + "\n" for key, value in table.items()) for name, table in configuration.items())
    (work / "seatrial.toml").write_text(config)
    report = root / "evidence" / "report.json"
    with (root / "evidence" / "run.log").open("xb") as output:
        command = [str(binary), "run", "--report", str(report)]
        for case in cases:
            command.extend(["--case", case])
        completed = subprocess.run(command, cwd=work, env=env, stdout=output, stderr=subprocess.STDOUT, timeout=600)
    result = {"state": "executed", "exit": completed.returncode, "cases": cases, "scope": "declared" if mode == "declared" else "diagnostic", "scenes": specification["scenes"], "product_pointer": current}
    (root / "evidence" / "execution.json").write_text(json.dumps(result, indent=2) + "\n")
    return result
