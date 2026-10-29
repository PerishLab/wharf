import hashlib
import json
import tempfile
from pathlib import Path

from lib.content import canonical
from lib.process.trial import admission, context, judgment, preparation, publication, transport
from lib.process.trial.admission import checked
from lib.refusal import Refusal
from lib.store import r2

STEPS = ("[windtun] load attempt", "[windtun] published artifacts", "[windtun] judge observation", "[windtun] record attempt")


def runner_identity(installed):
    return {"version": installed["version"], "sha256": installed["sha256"], "pointer": canonical.digest(installed["pointer"]), "seal": installed["pointer"]["seal"]}


def publisher(prepared):
    admitted = prepared["admission"]
    source = prepared["source"]
    completed = admitted["distribution"]["completed"]
    run = context.api(f"actions/runs/{completed['run']}/attempts/{completed['attempt']}")
    selected = admission.candidate(run, completed["run"], source)
    record = admission.document(f"{admitted['authority']}/v1/releases/{admitted['channel']}/{admitted['marker']}/distribution.json")
    current = admission.publication(run, selected, record)
    checked(current["distribution"] == admitted["distribution"] and current["trial"] == admitted["trial"], "publication changed after preparation")


def qualify(prepared, evidence, producer):
    context.succeeded(producer, STEPS)
    preparation.unchanged(prepared)
    publisher(prepared)
    execution = json.loads((evidence / "execution.json").read_bytes())
    report = json.loads((evidence / "report.json").read_bytes())
    runner = json.loads((evidence / "runner.json").read_bytes())
    observed = json.loads((evidence / "observation.json").read_bytes())
    checked(observed.get("ok") is True and observed.get("state") == "passed", "producer observation did not pass")
    checked(prepared["requirements"]["scope"] == "declared", "diagnostic execution cannot qualify a suite")
    admitted = prepared["admission"]
    seal = publication.verified(prepared["product"]["pointer"]["seal"], admitted["authority"])
    checked(seal == prepared["product"]["seal"], "public seal changed after preparation")
    raw = admission.fetch(prepared["product"]["pointer"]["seal"]["url"])
    checked(hashlib.sha256(raw).hexdigest() == prepared["product"]["pointer"]["seal"]["sha256"], "tested public seal differs from frozen digest")
    judgment.identity(report, admitted, raw)
    judgment.complete(report, execution, runner)
    preparation.completed(prepared, report, execution, runner)
    return {"schema": "wharf.windtun.pass/v1", "policy": context.POLICY, "implementation": canonical.digest(context.fingerprint()), "observer": prepared["observer"], "source": prepared["source"], "publisher": admitted["distribution"]["completed"], "distribution": canonical.digest(admitted["distribution"]), "declaration": admitted["trial"], "requirements": prepared["requirements"], "prepared": canonical.digest(prepared), "runner": runner_identity(runner), "scene": report["scene"], "groups": report["groups"], "products": report["products"], "images": report["images"], "controls": prepared["controls"], "cleanup": True}


def current(prepared):
    preparation.unchanged(prepared)
    source = prepared["source"]
    _, authority, root = publication.location(source["repository"], source["marker"])
    index = admission.document(f"{authority}/{root}/latest.json")
    checked(index.get("schema") == "wharf.windtun.index/v1", "unknown qualification discovery")
    observer = context.decode(index.get("observer"))
    path = publication.path(source["repository"], source["marker"], observer)
    checked(index.get("intent", {}).get("url") == f"{authority}/{path}/intent.json", "qualification discovery points to another attempt")
    intent = publication.verified(index["intent"], authority)
    checked(intent.get("observer") == observer and intent.get("started") == index.get("started") and intent.get("source") == source and intent.get("scope") == "declared" and intent.get("policy") == context.POLICY, "qualification intent differs from current scope")
    result = admission.document(f"{authority}/{path}/result.json")
    checked(result.get("schema") == "wharf.windtun.result/v1" and result.get("ok") is True and result.get("state") == "passed" and result.get("observer") == observer and result.get("source") == source and result.get("intent") == canonical.digest(intent), "latest attempt has no current suite qualification")
    checked(result.get("credential", {}).get("url") == f"{authority}/{path}/pass.json", "qualification result points to another credential")
    passed = publication.verified(result["credential"], authority)
    checked(passed.get("schema") == "wharf.windtun.pass/v1" and passed.get("observer") == observer and passed.get("intent") == canonical.digest(intent) and passed.get("source") == source, "qualification credential belongs to another attempt")
    checked(passed.get("policy") == context.POLICY and passed.get("implementation") == canonical.digest(context.fingerprint()), "qualification policy or implementation is invalidated")
    checked(passed.get("requirements") == prepared["requirements"] and passed.get("declaration") == prepared["admission"]["trial"] and passed.get("distribution") == canonical.digest(prepared["admission"]["distribution"]), "qualification requirements or publication changed")
    runner = passed.get("runner", {})
    checked(runner.get("pointer") == canonical.digest(prepared["runner"]["pointer"]) and runner.get("seal") == prepared["runner"]["pointer"]["seal"] and context.digest(runner.get("sha256")), "qualification runner differs from current official stable")
    checked(passed.get("scene") == prepared["scene"] and passed.get("cleanup") is True and passed.get("evidence") == result.get("evidence"), "qualification world or evidence differs")
    checked(passed.get("images") == [prepared["image"]] and passed.get("controls") == prepared["controls"], "qualification runtime images differ from current requirements")
    checked(admission.document(f"{authority}/{root}/latest.json") == index, "qualification discovery changed during readback")
    return passed


def assessment(prepared, evidence, producer):
    try:
        credential = qualify(prepared, evidence, producer)
        return {"state": "passed", "ok": True}, credential
    except (Refusal, OSError, ValueError, KeyError, TypeError) as failure:
        state = "unknown"
        if producer.get("conclusion") in ("failure", "cancelled", "timed_out"):
            state = "cancelled" if producer["conclusion"] == "cancelled" else "failed"
        return {"state": state, "ok": False, "reason": str(failure) if isinstance(failure, Refusal) else "execution evidence is incomplete or malformed"}, None


def issue(held):
    observer, jobs = context.observe(held["observer"], "windtun-issue")
    context.succeeded(jobs.get(context.NAMES["windtun-prepare"], {}), ["[windtun] admit publisher", "[windtun] prepare attempt"])
    intent = publication.read(held["repository"], held["marker"], observer)
    producer = jobs.get(context.NAMES["windtun"], {})
    bucket = r2.configured()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        prepared = transport.resolve(bucket, intent, root / "prepared")
        checked(prepared["started"] == jobs[context.NAMES["windtun-prepare"]].get("started_at"), "prepared start differs from native producer")
        result, passed = {"state": "unknown", "ok": False, "reason": "producer has no verified collection output"}, None
        if context.digest(held["digest"]):
            evidence = root / "evidence"
            record, digest = transport.retrieve(bucket, prepared, "execution", evidence)
            checked(context.bundle(record["files"], observer) == held["digest"], "native collector output differs from recorded evidence")
            result, passed = assessment(prepared, evidence, producer)
            result["evidence"] = {"key": context.address(prepared, "execution"), "record": digest, "digest": held["digest"]}
            if passed is not None:
                passed["evidence"] = result["evidence"]
                passed["intent"] = canonical.digest(intent)
        elif producer.get("conclusion") == "cancelled":
            result["state"] = "cancelled"
    name, authority, _ = publication.location(held["repository"], held["marker"])
    return publication.finish(r2.writer(name, "RELEASES"), authority, intent, (result, passed))
