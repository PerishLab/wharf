from lib.content.lane import codec, preview
from lib.content.static import admission
from lib.content.static.github import LOGIN
from lib.process.lane import entry, execution, reservation
from lib.refusal import Refusal
from lib.store.lane import documents

CONCLUSIONS = {"success", "failure", "cancelled", "timed_out", "skipped", "neutral", "action_required", "stale", "startup_failure"}
STATE = "[record] evidence lane / [record] state lane"


def positive(value):
    if type(value) is not int or not 1 <= value <= 18446744073709551615:
        raise Refusal("lane original provider identity must be a bounded positive integer")
    return value


def actors(value):
    preview.shape(value, {"actor", "triggering_actor"}, "retained original actors")
    result = {}
    for key in ("actor", "triggering_actor"):
        user = value[key]
        preview.shape(user, {"id", "login"}, "retained original actor")
        preview.matches(user["login"], LOGIN, "retained original login")
        result[key] = {"id": positive(user["id"]), "login": user["login"]}
    return result


def basis(intent, retained):
    intent = entry.requested(documents.encode(intent).decode())
    retained = documents.decode(documents.encode(retained))
    preview.shape(retained, {"request", "custody", "actors"}, "retained original provenance")
    requested = codec.read("request", retained["request"])
    if intent["operation"] == "inspect" or requested != reservation.requested(intent, requested["intent"].get("artifact")):
        raise Refusal("lane original provenance needs the exact retained mutation request")
    custody = retained["custody"]
    preview.shape(custody, {"schema", "request", "commit", "run", "attempt"}, "retained original custody")
    if custody["schema"] != "wharf.lane.custody/v1" or custody["request"] != codec.digest("request", requested):
        raise Refusal("lane original custody differs from the exact retained request")
    preview.matches(custody["commit"], preview.HEX[40], "original control commit")
    for key in ("run", "attempt"):
        positive(custody[key])
    observed = actors(retained["actors"])
    policy = entry.policy()
    if intent["caller"] != observed["actor"]["login"] or intent["workflow"] != f"{policy['repository']}/{policy['workflow']}@{custody['commit']}":
        raise Refusal("lane original dispatch caller or workflow differs from retained provenance")
    return intent, dict(retained, request=requested, actors=observed)


def run(custody, expected, api, control):
    value = api.run(custody["run"])
    if not isinstance(value, dict) or positive(value.get("id")) != custody["run"] or positive(value.get("run_attempt")) != custody["attempt"]:
        raise Refusal("lane original run or latest attempt moved")
    policy = entry.policy()
    for key in ("repository", "head_repository"):
        held = value.get(key)
        if not isinstance(held, dict) or positive(held.get("id")) != control["id"] or held.get("full_name") != control["name"]:
            raise Refusal("lane original control repository or fork differs")
    if value.get("event") != "workflow_dispatch" or value.get("path") != policy["workflow"] or value.get("head_branch") != policy["branch"] or value.get("head_sha") != custody["commit"]:
        raise Refusal("lane original control workflow, branch or commit differs")
    if value.get("status") != "completed" or not isinstance(value.get("conclusion"), str) or value["conclusion"] not in CONCLUSIONS:
        raise Refusal("lane original run is not explicitly terminal")
    observed = {}
    for key in ("actor", "triggering_actor"):
        held = value.get(key)
        if not isinstance(held, dict):
            raise Refusal("lane original actor identity is absent")
        observed[key] = {"id": held.get("id"), "login": held.get("login")}
    if actors(observed) != expected:
        raise Refusal("lane original actors differ from independently retained identities")
    return {"repository": control, "run": custody["run"], "attempt": custody["attempt"], "commit": custody["commit"],
            "path": policy["workflow"], "actors": observed, "conclusion": value["conclusion"]}


def jobs(intent, custody, api):
    value = api.jobs(custody["run"], custody["attempt"])
    if not isinstance(value, dict) or type(value.get("total_count")) is not int or not isinstance(value.get("jobs"), list):
        raise Refusal("lane original native jobs are unavailable or malformed")
    held = value["jobs"]
    if value["total_count"] != len(held) or not 1 <= len(held) <= 30:
        raise Refusal("lane original job inventory is incomplete or exceeds its single-page budget")
    expected = set(execution.producer_names(intent["operation"])) | {STATE}
    observed, identifiers = {}, set()
    inactive = "[deploy] cfworker disposal" if intent["operation"] == "apply" else "[deploy] cfworker lane"
    for job in held:
        if not isinstance(job, dict) or not isinstance(job.get("name"), str):
            raise Refusal("lane original native job is malformed")
        identifier = positive(job.get("id"))
        if positive(job.get("run_id")) != custody["run"] or positive(job.get("run_attempt")) != custody["attempt"] or job.get("head_sha") != custody["commit"] or job.get("head_branch") != entry.policy()["branch"]:
            raise Refusal("lane original native job belongs to another run, attempt, branch or head")
        if job.get("status") != "completed" or not isinstance(job.get("conclusion"), str) or job["conclusion"] not in CONCLUSIONS:
            raise Refusal("lane original native job is not explicitly terminal")
        name = job["name"]
        if name == inactive and job["conclusion"] == "skipped":
            name += " / " + inactive
        if name == inactive + " / " + inactive and job["conclusion"] != "skipped":
            raise Refusal("lane original inactive native branch was not skipped")
        if name not in expected or name in observed or identifier in identifiers:
            raise Refusal("lane original native job set is foreign or ambiguous")
        observed[name] = {"id": identifier, "name": name, "conclusion": job["conclusion"]}
        identifiers.add(identifier)
    if set(observed) != expected:
        raise Refusal("lane original native job set is incomplete")
    return [observed[key] for key in sorted(observed)]


def observe(intent, retained, api):
    intent, retained = basis(intent, retained)
    policy = entry.policy()
    repository = api.repository(policy["repository"])
    control = admission.repository(repository, policy["repository"])
    if repository.get("default_branch") != policy["branch"]:
        raise Refusal("lane original control default branch differs")
    initial = run(retained["custody"], retained["actors"], api, control)
    inventory = jobs(intent, retained["custody"], api)
    if run(retained["custody"], retained["actors"], api, control) != initial:
        raise Refusal("lane original run facts moved during native job observation")
    return documents.decode(documents.encode({"schema": "wharf.lane.original/v1", "request": codec.digest("request", retained["request"]),
                            "custody": retained["custody"], "workflow": initial, "jobs": inventory}))
