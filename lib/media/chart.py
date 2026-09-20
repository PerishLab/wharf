import re
import subprocess
import tempfile
from pathlib import Path

from lib.content import resources
from lib.process import git, run
from lib.refusal import Refusal

HOST = resources.read_json("registries.json")["oci"]["host"]
ROOT = "charts"


def repository(owner):
    return f"oci://{HOST}/{owner.lower()}/{ROOT}"


def charts(source):
    listed = git(source, "ls-files", f"{ROOT}/*/Chart.yaml").splitlines()
    found = []
    for path in sorted(listed):
        text = (Path(source) / path).read_text()
        name = re.search(r"^name:\s*(\S+)\s*$", text, re.M)
        declared = re.search(r"^version:\s*[\"']?([^\"'\s]+)", text, re.M)
        if not name or not declared or declared.group(1) != "0.0.0":
            raise Refusal(f"{path} must declare a name and version 0.0.0")
        found.append({"name": name.group(1), "path": path.rsplit("/", 1)[0]})
    return found


def exists(owner, name, version, runner=subprocess.run):
    argv = ["helm", "show", "chart", f"{repository(owner)}/{name}", "--version", version]
    return runner(argv, capture_output=True).returncode == 0


def pending(source, owner, version):
    return [dict(chart, published=exists(owner, chart["name"], version)) for chart in charts(source)]


def publish(source, owner, version, runner=run):
    results = []
    for chart in pending(source, owner, version):
        if chart["published"]:
            results.append({"name": chart["name"], "state": "already-published"})
            continue
        with tempfile.TemporaryDirectory() as directory:
            runner(["helm", "package", chart["path"], "--version", version, "--app-version", version, "--destination", directory], source)
            archive = Path(directory) / f"{chart['name']}-{version}.tgz"
            if not archive.is_file():
                raise Refusal(f"helm did not produce {archive.name}")
            runner(["helm", "push", str(archive), repository(owner)], source)
        if not exists(owner, chart["name"], version):
            raise Refusal(f"{chart['name']} {version} is not visible after pushing")
        results.append({"name": chart["name"], "state": "published"})
    return {"version": version, "repository": repository(owner), "charts": results}
