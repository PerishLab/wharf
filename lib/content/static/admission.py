import os
import re

from lib.content import canonical, resources
from lib.content.lane import preview
from lib.content.static import evidence, source
from lib.content.static.github import LOGIN, GitHub, Routing
from lib.refusal import Refusal
from lib.store import r2


def requested(body):
    if not isinstance(body, str):
        raise Refusal("Preview entry request must be a bounded JSON string")
    try:
        encoded = body.encode()
    except UnicodeError as error:
        raise Refusal("Preview entry request is not valid UTF-8 text") from error
    if len(encoded) > 65536:
        raise Refusal("Preview entry request exceeds its bounded document budget")
    intent = preview.request(evidence.decode(encoded))
    preview.matches(intent["repository"], re.compile(r"PerishLab/[A-Za-z0-9_-]+"), "entry repository")
    return intent


def context(environ=os.environ):
    policy = resources.read_json("build.json")["admission"]
    expected = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REPOSITORY": policy["repository"],
                "GITHUB_REF": "refs/heads/" + policy["branch"],
                "GITHUB_WORKFLOW_REF": f"{policy['repository']}/{policy['workflow']}@refs/heads/{policy['branch']}"}
    if any(environ.get(key) != value for key, value in expected.items()):
        raise Refusal("Preview entry requires trusted exact main workflow context")
    commit = environ.get("GITHUB_SHA")
    preview.matches(commit, preview.HEX[40], "platform control commit")
    if environ.get("GITHUB_WORKFLOW_SHA") != commit:
        raise Refusal("Preview workflow source differs from its platform control commit")
    result = {"commit": commit}
    for key, name in (("run", "GITHUB_RUN_ID"), ("attempt", "GITHUB_RUN_ATTEMPT")):
        value = environ.get(name)
        if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", value):
            raise Refusal("Preview platform run and attempt must be bounded positive identities")
        result[key] = int(value)
    return result


def observe(intent, environ=os.environ):
    preview.request(intent)
    preview.matches(intent["repository"], re.compile(r"PerishLab/[A-Za-z0-9_-]+"), "entry repository")
    platform = context(environ)
    bucket = r2.registration({key: intent[key] for key in ("repository", "app")}, environ)
    control = GitHub(r2.secret(environ, "WHARF_PREVIEW_CONTROL_TOKEN"))
    product = GitHub(r2.secret(environ, "WHARF_PREVIEW_PRODUCT_TOKEN"))
    return admit(intent, bucket, platform, Routing(intent["repository"], control, product))


def registration(bucket, intent):
    preview.request(intent)
    body = bucket.get(f"preview/v1/registrations/{intent['repository']}/{intent['app']}.json")
    if not isinstance(body, bytes) or len(body) > 65536:
        raise Refusal("Preview registration must be a bounded trusted-store document")
    target = evidence.decode(body)
    preview.admitted(intent, target)
    return target


def identity(value, name):
    if not isinstance(value, dict) or type(value.get("id")) is not int or value["id"] < 1:
        raise Refusal(f"Preview {name} requires a positive provider identity")
    return value["id"]


def repository(value, name):
    identity(value, "repository")
    if value.get("full_name") != name or value.get("archived") is not False:
        raise Refusal("Preview repository identity is foreign, archived or incomplete")
    return {"id": value["id"], "name": name}


def actors(api, run, name):
    result = {}
    for key in ("actor", "triggering_actor"):
        user = run.get(key)
        identifier = identity(user, key)
        preview.matches(user.get("login"), LOGIN, "observed GitHub actor")
        held = api.permission(name, user["login"])
        actual = held.get("user")
        if identity(actual, "permission actor") != identifier or actual.get("login") != user["login"]:
            raise Refusal("Preview permission belongs to another provider actor")
        permissions = actual.get("permissions")
        if held.get("permission") not in ("write", "admin") or not isinstance(permissions, dict) or permissions.get("push") is not True:
            raise Refusal("Preview observed actor lacks repository push permission")
        result[key] = {"id": identifier, "login": user["login"], "permission": held["permission"]}
    return result


def workflow(api, context):
    preview.shape(context, {"run", "attempt", "commit"}, "trusted run context")
    for key in ("run", "attempt"):
        if type(context[key]) is not int or context[key] < 1:
            raise Refusal("Preview trusted run and attempt must be positive")
    preview.matches(context["commit"], preview.HEX[40], "trusted control commit")
    policy = resources.read_json("build.json")["admission"]
    control = api.repository(policy["repository"])
    expected = repository(control, policy["repository"])
    run = api.run(context["run"])
    identity(run, "workflow run")
    if control.get("default_branch") != policy["branch"] or run["id"] != context["run"] or type(run.get("run_attempt")) is not int or run["run_attempt"] != context["attempt"]:
        raise Refusal("Preview workflow run/attempt or default branch differs from trusted context")
    for key in ("repository", "head_repository"):
        observed = run.get(key)
        if identity(observed, key) != expected["id"] or observed.get("full_name") != expected["name"]:
            raise Refusal("Preview workflow repository or fork identity differs")
    if run.get("event") != "workflow_dispatch" or run.get("path") != policy["workflow"] or run.get("head_branch") != policy["branch"] or run.get("head_sha") != context["commit"] or run.get("status") != "in_progress":
        raise Refusal("Preview requires its exact active main workflow, not release or caller-authored identity")
    return run, {"repository": expected, "run": context["run"], "attempt": context["attempt"], "commit": context["commit"], "path": policy["workflow"]}


def admit(intent, bucket, context, api):
    target = registration(bucket, intent)
    run, control = workflow(api, context)
    observed = actors(api, run, intent["repository"])
    expected = f"{control['repository']['name']}/{control['path']}@{control['commit']}"
    if intent["caller"] != observed["actor"]["login"] or intent["workflow"] != expected:
        raise Refusal("Preview request caller/workflow does not match independent provider observations")
    product = repository(api.repository(intent["repository"]), intent["repository"])
    origin = None
    if intent["operation"] == "apply":
        commit = api.commit(intent["repository"], intent["source"]["commit"])
        tree = commit.get("tree")
        if commit.get("sha") != intent["source"]["commit"] or not isinstance(tree, dict) or tree.get("sha") != intent["source"]["tree"]:
            raise Refusal("Preview remote commit/tree does not match the exact request")
        origin = {"commit": commit["sha"], "tree": tree["sha"]}
    return {"schema": "wharf.preview.admission/v1", "intent": canonical.digest(intent), "registration": canonical.digest(target), "target": target, "workflow": control, "actors": observed, "repository": product, "source": origin}


def qualify(root, intent, observation, env):
    preview.request(intent)
    preview.shape(observation, {"schema", "intent", "registration", "target", "workflow", "actors", "repository", "source"}, "trusted admission observation")
    if intent["operation"] != "apply":
        raise Refusal("Preview inspect/discard do not qualify product source")
    if not isinstance(observation, dict) or observation.get("schema") != "wharf.preview.admission/v1" or observation.get("intent") != canonical.digest(intent) or observation.get("registration") != intent["registration"] or observation.get("source") != {key: intent["source"][key] for key in ("commit", "tree")}:
        raise Refusal("Preview source observation belongs to another request")
    configuration = source.qualify(root, intent, observation["target"], env)
    source.fresh(root, env)
    return configuration
