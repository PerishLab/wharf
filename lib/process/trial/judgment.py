import hashlib
import json
import math
from pathlib import Path

from lib.media.release import fetch
from lib.process.trial import preparation
from lib.process.trial.admission import checked

STAGES = ("configuration", "scene", "selection", "lane", "artifacts", "preparation", "execution", "cleanup")


def positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def identity(report, expected, seal):
    products = report.get("products")
    checked(isinstance(products, list) and len(products) == 1, "product selection differs")
    product = products[0]
    checked(product.get("name") == expected["product"] and product.get("channel") == expected["channel"], "tested product identity differs")
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
    selected = execution["cases"]
    cases = report.get("cases")
    checked(sorted(report.get("selected", [])) == sorted(selected) and isinstance(cases, list) and len(cases) == len(selected) and selected and len(set(selected)) == len(selected), "completed selection differs")
    checked(sorted(case.get("name", "") for case in cases) == sorted(selected), "completed cases differ from selected cases")
    checked(all(case.get("passed") is True and case.get("error") is None and positive(case.get("seconds")) for case in cases), "case evidence is incomplete")
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
    checked(isinstance(scene, dict) and scene.get("kind") == "scene" and all(scene.get(name) == value for name, value in execution["scenes"].items() if name != "authority") and str(scene.get("manifest", {}).get("url", "")).startswith(execution["scenes"]["authority"] + "/"), "Depot scene identity is missing")
    images = report.get("images")
    checked(isinstance(images, list) and any(image.get("name") == report["products"][0]["name"] and "@sha256:" in str(image.get("digest")) for image in images), "runtime image digest is missing")


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
        url = f"{admitted['authority']}/v1/releases/{admitted['channel']}/{admitted['marker']}/seal.json"
        identity(report, admitted, fetch(url))
        if report.get("state") in ("failed", "cancelled"):
            result["state"] = report["state"]
        complete(report, execution, runner)
        prepared = json.loads((evidence / "prepared.json").read_bytes())
        preparation.completed(prepared, report, execution, runner)
        if execution.get("scope") != "declared":
            result["state"] = "diagnostic"
            return result
        checked(sorted(execution["cases"]) == sorted(admitted["trial"]["specification"]["cases"]), "execution does not cover the declared suite")
        result.update(ok=True, state="passed")
        return result
    finally:
        (evidence / "observation.json").write_text(json.dumps(result, indent=2) + "\n")
