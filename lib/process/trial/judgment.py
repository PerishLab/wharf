import hashlib
import json
import math
from pathlib import Path

from lib.media.release import fetch
from lib.process.trial.admission import AUTHORITY, checked

STAGES = ("configuration", "scene", "selection", "lane", "artifacts", "preparation", "execution", "cleanup")


def positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def identity(report, expected, seal):
    products = report.get("products")
    checked(isinstance(products, list) and len(products) == 1, "product selection differs")
    product = products[0]
    checked(product.get("name") == "plumb" and product.get("channel") == expected["channel"], "tested product identity differs")
    checked(product.get("version") == expected["marker"] and product.get("commit") == expected["commit"], "tested product is not this publisher's release")
    checked(product.get("seal") == hashlib.sha256(seal).hexdigest(), "tested seal differs")
    held = json.loads(seal)
    checked(all(held.get(name) == expected.get(field) for name, field in (("product", "product"), ("channel", "channel"), ("releaseVersion", "marker"), ("commit", "commit"))), "public seal identity differs")
    checked(product.get("archive") == held["artifacts"]["linux-x64"]["sha256"], "tested archive differs")


def complete(report, execution, runner):
    checked(report.get("schema") == "seatrial.run/v1" and report.get("platform") == "linux-x86_64", "report format or platform differs")
    checked(report.get("runner") == runner.get("sha256"), "report runner differs from installed runner")
    checked(execution.get("exit") == 0 and report.get("ok") is True and report.get("state") == "passed", "black-box execution did not pass")
    checked(report.get("cleanup") is True, "lane cleanup is not complete")
    selected = [execution["case"]]
    cases = report.get("cases")
    checked(report.get("selected") == selected and isinstance(cases, list) and len(cases) == 1, "completed selection differs")
    checked(cases[0].get("name") == selected[0] and cases[0].get("passed") is True and cases[0].get("error") is None, "case evidence is incomplete")
    checked(positive(report.get("seconds")) and positive(cases[0].get("seconds")), "runtime timing is invalid")
    stages = report.get("stages", [])
    checked([stage.get("name") for stage in stages] == list(STAGES), "stage timing is incomplete")
    end = 0
    for stage in stages:
        checked(stage.get("ok") is True and positive(stage.get("start")) and positive(stage.get("seconds")), "stage failed or has invalid timing")
        checked(stage["start"] >= end, "stage timings overlap or run backwards")
        end = stage["start"] + stage["seconds"]
    checked(end <= report["seconds"], "stage timing exceeds total runtime")
    scene = report.get("scene")
    checked(isinstance(scene, dict) and scene.get("product") == "seatrial" and scene.get("kind") == "scene" and scene.get("channel") == execution["scene_channel"], "Depot scene identity is missing")
    images = report.get("images")
    checked(isinstance(images, list) and any(image.get("name") == "plumb" and "@sha256:" in str(image.get("digest")) for image in images), "runtime image digest is missing")


def judge(held):
    evidence = Path(held["source"]).resolve() / "evidence"
    result = {"schema": "wharf.trial.observation/v1", "ok": False, "state": "unknown"}
    try:
        admitted = json.loads((evidence / "admission.json").read_text())
        execution = json.loads((evidence / "execution.json").read_text())
        checked(admitted.get("eligible") is True, "publisher is not eligible")
        if execution.get("state") == "superseded":
            result.update(state="superseded", execution=execution)
            return result
        checked(execution.get("state") == "executed", "execution is incomplete")
        report = json.loads((evidence / "report.json").read_text())
        runner = json.loads((evidence / "runner.json").read_text())
        url = f"{AUTHORITY}/v1/releases/{admitted['channel']}/{admitted['marker']}/seal.json"
        identity(report, dict(admitted, product="plumb"), fetch(url))
        if report.get("state") in ("failed", "cancelled"):
            result["state"] = report["state"]
        complete(report, execution, runner)
        result.update(ok=True, state="passed")
        return result
    finally:
        (evidence / "observation.json").write_text(json.dumps(result, indent=2) + "\n")
