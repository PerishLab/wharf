import json
import os
import signal
import subprocess
import tempfile
import time

from lib.identity.bind.spec import entry, require
from lib.media import release

AUTHORITY = "https://releases.plumb.perish.uk"
LIMIT = 4194304


def capture(arguments):
    environment = {name: os.environ[name] for name in ("PATH", "HOME", "LANG", "GH_TOKEN") if name in os.environ}
    with tempfile.TemporaryFile() as output:
        child = subprocess.Popen(["plumb", *arguments], stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL, env=environment, start_new_session=True)
        try:
            deadline = time.monotonic() + 180
            while child.poll() is None:
                require(time.monotonic() < deadline and os.fstat(output.fileno()).st_size <= LIMIT, "qualification reader exceeded its time or output budget")
                time.sleep(0.02)
            require(os.fstat(output.fileno()).st_size <= LIMIT, "qualification reader output exceeds bounds")
            output.seek(0)
            return child.returncode, json.load(output)
        finally:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()


def identity():
    pointer = json.loads(release.fetch(AUTHORITY + "/v1/channels/stable.json"))
    require(pointer.get("product") == "plumb" and pointer.get("channel") == "stable", "qualification reader stable identity differs")
    ref = pointer["seal"]
    require(ref["url"] == f"{AUTHORITY}/v1/releases/stable/{pointer['releaseVersion']}/seal.json" and 0 < ref["size"] <= LIMIT, "qualification reader seal reference differs")
    body = release.fetch(ref["url"])
    require(entry(body) == {name: ref[name] for name in ("size", "sha256")}, "qualification reader seal bytes differ")
    seal = json.loads(body)
    require(all(seal[name] == pointer[name] for name in ("product", "channel", "releaseVersion", "commit")), "qualification reader pointer and seal disagree")
    status, actual = capture(["release", "authority", "--json"])
    require(status == 0 and actual == seal["guard"] and actual["producer"] == f"{pointer['releaseVersion']}@{pointer['commit']}", "qualification reader is not current formal stable")
    return actual


def check(repository, reference):
    authority = identity()
    status, report = capture(["windtun", "check", "--repository", repository, "--marker", reference, "--json"])
    require(isinstance(report, dict) and set(report) == {"schema", "subject", "state", "qualified", "platform", "association", "promotion", "evidence", "reason"}, "qualification report fields differ")
    require(report["schema"] == "plumb.windtun.qualification/v1" and report["subject"] == {"repository": repository, "marker": reference}, "qualification report names another subject")
    require(report["promotion"] is False and report["association"] is None and report["platform"] == "linux-x86_64", "qualification report exceeds its role")
    if status == 3 and report["state"] == "unknown" and report["qualified"] is False:
        raise OSError("current qualification is unknown")
    require(status == 0 and report["state"] == "qualified" and report["qualified"] is True and report["reason"] is None, "latest public qualification is not a complete current success")
    return {"reader": authority, "report": report}


def pins(qualified):
    evidence = qualified["report"]["evidence"]
    return {"reader": qualified["reader"], "requirements": evidence["final_requirements"]["requirements"], **{name: evidence[name] for name in ("credential", "intent", "source", "tag", "observer", "world")}}
