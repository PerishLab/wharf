import json
import re
import subprocess

from lib.content import canonical
from lib.media import oci
from lib.process.trial import context, publication
from lib.process.trial.admission import checked, document
from lib.process.trial.declaration import NAME, shape, validate
from lib.refusal import Refusal

RUNNER = "https://releases.seatrial.perish.uk"
PROBE = "docker.io/curlimages/curl:8.10.1@sha256:d9b4541e214bcd85196d6e92e2753ac6d0ea699f0af5741f8c6cccbfcf00ef4b"
FIELDS = ("schema", "policy", "observer", "started", "source", "admission", "requirements", "product", "runner", "scene", "image", "controls")


def registry(argv, cwd):
    try:
        return subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True, timeout=45).stdout
    except subprocess.TimeoutExpired as failure:
        raise Refusal("public runtime image inspection exceeded its budget") from failure


def image(admitted):
    reference = admitted["trial"]["specification"]["image"]
    if "@" not in reference and ":" not in reference.rsplit("/", 1)[-1]:
        reference += ":" + admitted["marker"].removeprefix("v")
    digest = oci.public_digest(reference, registry)
    checked(re.fullmatch(r"sha256:[0-9a-f]{64}", digest), "runtime registry digest is invalid")
    repository = reference.split("@", 1)[0]
    if ":" in repository.rsplit("/", 1)[-1]:
        repository = repository.rsplit(":", 1)[0]
    return {"name": admitted["product"], "reference": reference, "digest": repository + "@" + digest}


def product(admitted):
    authority = admitted["authority"]
    pointer = document(f"{authority}/v1/channels/{admitted['channel']}.json")
    checked(all(pointer.get(name) == admitted[field] for name, field in (("product", "product"), ("channel", "channel"), ("releaseVersion", "marker"), ("commit", "commit"))), "product channel moved from the admitted publication")
    seal = publication.verified(pointer["seal"], authority)
    checked(all(seal.get(name) == pointer.get(name) for name in ("product", "channel", "releaseVersion", "commit")), "product pointer and seal differ")
    return {"pointer": pointer, "seal": seal}


def runner():
    pointer = document(f"{RUNNER}/v1/channels/stable.json")
    checked(pointer.get("product") == "seatrial" and pointer.get("channel") == "stable", "runner must follow official stable")
    seal = publication.verified(pointer["seal"], RUNNER)
    checked(all(seal.get(name) == pointer.get(name) for name in ("product", "channel", "releaseVersion", "commit")), "runner pointer and seal differ")
    return {"pointer": pointer, "seal": seal}


def scene(specification):
    scenes = specification["scenes"]
    pointer = document(f"{scenes['authority']}/channels/{scenes['channel']}/scenes/latest.json")
    checked(pointer.get("format") == 3 and pointer.get("kind") == "scene" and all(pointer.get(name) == scenes[name] for name in ("product", "channel")), "scene channel identity differs")
    checked(context.digest(pointer.get("generation")), "scene generation is missing")
    publication.verified(pointer["manifest"], scenes["authority"])
    return pointer


def freeze(admitted, observer, mode, started):
    context.clock(started)
    checked(admitted.get("eligible") is True, "preparation requires an admitted publisher")
    specification = validate(admitted["trial"]["specification"])
    checked(mode == "declared" or isinstance(mode, str) and NAME.fullmatch(mode), "invalid diagnostic selection")
    requirements = {"platform": "linux-x86_64", "declared": specification["cases"], "selected": specification["cases"] if mode == "declared" else [mode], "scope": "declared" if mode == "declared" else "diagnostic"}
    return {"schema": "wharf.windtun.prepared/v1", "policy": context.POLICY, "observer": context.decode(observer), "started": started, "source": admitted["trial"]["source"], "admission": admitted, "requirements": requirements, "product": product(admitted), "runner": runner(), "scene": scene(specification), "image": image(admitted), "controls": {"probe": PROBE}}


def validate_prepared(value):
    shape(value, FIELDS, "Windtun preparation")
    checked(value["schema"] == "wharf.windtun.prepared/v1" and value["policy"] == context.POLICY, "unknown preparation policy")
    context.decode(value["observer"])
    context.clock(value["started"])
    admitted = value["admission"]
    checked(admitted.get("eligible") is True and value["source"] == admitted["trial"]["source"], "prepared source differs from admission")
    requirements = value["requirements"]
    shape(requirements, ("platform", "declared", "selected", "scope"), "Windtun requirements")
    specification = validate(admitted["trial"]["specification"])
    checked(requirements["platform"] == "linux-x86_64" and requirements["declared"] == specification["cases"], "prepared coverage differs from declaration")
    checked(requirements["scope"] in ("declared", "diagnostic"), "unknown preparation scope")
    checked(value["controls"] == {"probe": PROBE}, "prepared probe control differs")
    selected = requirements["selected"]
    checked(isinstance(selected, list) and selected and all(isinstance(name, str) and NAME.fullmatch(name) for name in selected) and len(set(selected)) == len(selected), "prepared selection is invalid")
    checked(selected == requirements["declared"] if requirements["scope"] == "declared" else len(selected) == 1, "prepared selection differs from scope")
    return value


def unchanged(prepared):
    validate_prepared(prepared)
    checked(product(prepared["admission"]) == prepared["product"], "product world moved after preparation")
    checked(runner() == prepared["runner"], "runner stable moved after preparation")
    checked(scene(prepared["admission"]["trial"]["specification"]) == prepared["scene"], "scene channel moved after preparation")
    checked(image(prepared["admission"]) == prepared["image"], "runtime image moved after preparation")


def completed(prepared, report, execution, installed):
    validate_prepared(prepared)
    requirements = prepared["requirements"]
    checked(report.get("platform") == requirements["platform"] and execution.get("cases") == requirements["selected"] and execution.get("scope") == requirements["scope"], "execution differs from prepared requirements")
    checked(execution.get("product_pointer") == prepared["product"]["pointer"] and report.get("scene") == prepared["scene"], "execution consumed another product or scene world")
    checked(all(installed.get(name) == value for name, value in prepared["runner"].items()), "installed runner differs from prepared official stable")
    groups = report.get("groups")
    checked(isinstance(groups, list) and groups and all(isinstance(group, dict) and context.digest(group.get("digest")) and isinstance(group.get("cases"), list) for group in groups), "resolved group identities are incomplete")
    cases = [name for group in groups for name in group["cases"]]
    checked(sorted(cases) == sorted(requirements["selected"]) and len(set(group["digest"] for group in groups)) == len(groups), "resolved groups do not cover exactly this selection")
    images = report.get("images")
    checked(isinstance(images, list) and images and all(isinstance(image, dict) and isinstance(image.get("name"), str) and re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", str(image.get("digest"))) and isinstance(image.get("reference"), str) and image["reference"] for image in images), "runtime image identities are incomplete")
    checked(len(set(image["name"] for image in images)) == len(images), "runtime image identities are duplicated")
    checked(images == [prepared["image"]], "runtime image differs from frozen selection or adds unknown coverage")
    return canonical.digest(prepared)
