import json
import re
import subprocess

from lib.content import canonical, marker
from lib.media import oci
from lib.process.trial import context, publication
from lib.process.trial.admission import checked, document
from lib.process.trial import declaration
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
    prepared = {"schema": "wharf.windtun.prepared/v" + str(declaration.version(specification)), "policy": context.POLICY, "observer": context.decode(observer), "started": started, "source": admitted["trial"]["source"], "admission": admitted, "requirements": requirements, "product": product(admitted), "runner": runner(), "scene": scene(specification), "image": image(admitted), "controls": {"probe": PROBE}}
    if "services" in specification:
        prepared["services"] = services(specification, admitted["product"])
    return prepared


def validate_prepared(value):
    shape(value, FIELDS + (("services",) if value.get("schema") == "wharf.windtun.prepared/v2" else ()), "Windtun preparation")
    checked(value["schema"] in ("wharf.windtun.prepared/v1", "wharf.windtun.prepared/v2") and value["policy"] == context.POLICY, "unknown preparation policy")
    context.decode(value["observer"])
    context.clock(value["started"])
    admitted = value["admission"]
    checked(admitted.get("eligible") is True and value["source"] == admitted["trial"]["source"], "prepared source differs from admission")
    requirements = value["requirements"]
    shape(requirements, ("platform", "declared", "selected", "scope"), "Windtun requirements")
    specification = validate(admitted["trial"]["specification"])
    checked(value["schema"] == "wharf.windtun.prepared/v" + str(declaration.version(specification)), "prepared schema differs from declaration")
    peers = declaration.services(specification)
    if peers:
        shape(value["services"], peers, "prepared services")
        for name, snapshot in value["services"].items():
            shape(snapshot, ("declaration", "pointer", "seal", "distribution", "source", "tag", "repository"), "prepared service")
            checked(snapshot["declaration"] == peers[name] and name != admitted["product"], "prepared service declaration differs")
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
    specification = prepared["admission"]["trial"]["specification"]
    checked(services(specification, prepared["admission"]["product"]) == prepared.get("services", {}), "service world moved after preparation")


def completed(prepared, report, execution, installed):
    validate_prepared(prepared)
    requirements = prepared["requirements"]
    combination(prepared, report)
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


def native(path):
    try:
        raw = subprocess.check_output(["gh", "api", "--hostname", "github.com", path], stderr=subprocess.PIPE, timeout=30)
        checked(0 < len(raw) <= 1048576, "service native metadata exceeds bounds")
        return json.loads(raw)
    except (OSError, subprocess.SubprocessError, ValueError) as failure:
        raise Refusal("service native metadata is unavailable or malformed") from failure


def source(repository, version):
    root = f"repos/{repository}"
    identity = native(root)
    expected = {"full_name": repository, "visibility": "public", "archived": False, "disabled": False}
    checked(all(type(identity.get(key)) is type(value) and identity[key] == value for key, value in expected.items()), "service repository authority differs")
    owner = identity.get("owner", {})
    checked(owner.get("id") == 330236468 and owner.get("node_id") == "O_kgDOE68CNA" and owner.get("login") == "PerishLab", "service repository owner differs")
    checked(type(identity.get("id")) is int and identity["id"] > 0 and isinstance(identity.get("node_id"), str) and identity["node_id"] and isinstance(identity.get("default_branch"), str) and identity["default_branch"], "service repository identity is incomplete")
    ref = native(f"{root}/git/ref/tags/{version}")
    checked(ref.get("ref") == f"refs/tags/{version}" and ref.get("object", {}).get("type") == "tag", "service needs its exact annotated tag")
    tag = ref["object"]["sha"]
    checked(isinstance(tag, str) and re.fullmatch(r"[0-9a-f]{40}", tag), "service tag identity is invalid")
    annotated = native(f"{root}/git/tags/{tag}")
    checked(annotated.get("sha") == tag and annotated.get("tag") == version and annotated.get("object", {}).get("type") == "commit", "service annotated tag differs")
    commit = annotated["object"]["sha"]
    checked(isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit), "service source commit is invalid")
    held = native(f"{root}/git/commits/{commit}")
    tree = held.get("tree", {}).get("sha")
    checked(held.get("sha") == commit and isinstance(tree, str) and re.fullmatch(r"[0-9a-f]{40}", tree), "service source tree differs")
    return {"source": {"repository": repository, "marker": version, "commit": commit, "tree": tree}, "tag": tag, "repository": {"id": identity["id"], "node": identity["node_id"], "name": repository, "default": identity["default_branch"]}}


def distribution(record, expected):
    fields = ("schema", "repository", "marker", "commit", "tree", "media", "state", "attempt", "completed", "resolution", "trial")
    shape(record, fields + (("plan",) if "plan" in record else ()), "service distribution")
    checked(record["schema"] == 1 and record["state"] == "complete" and all(record.get(key) == value for key, value in expected.items()), "service distribution is not complete at its native source")
    context.decode(record["attempt"])
    context.decode(record["completed"])
    shape(record["media"], ("binaries", "cargo", "oci", "npm", "chart", "cfworker", "channel"), "service media")
    checked(all(isinstance(state, str) and state for state in record["media"].values()) and record["media"]["binaries"] in ("published", "present") and isinstance(record["resolution"], dict), "service has no complete binary distribution")
    if "plan" in record:
        plan = record["plan"]
        shape(plan, ("schema", "repository", "marker", "commit", "tree", "wharf", "run", "attempt", "sha256"), "service plan")
        checked(plan["schema"] == "wharf.plan.reference/v1" and all(plan.get(key) == value for key, value in expected.items()) and context.digest(plan["sha256"]), "service plan source differs")
        context.decode({key: plan[key] for key in ("run", "attempt", "wharf")})
        checked(plan["run"] == record["completed"]["run"] and plan["wharf"] == record["completed"]["wharf"] and int(plan["attempt"]) <= int(record["completed"]["attempt"]), "service plan completion differs")


def service(name, selection):
    authority = selection["authority"]
    pointer = document(f"{authority}/v1/channels/stable.json")
    shape(pointer, ("schema", "product", "releaseVersion", "channel", "commit", "seal", "managers"), "service pointer")
    checked(pointer["schema"] == 1 and isinstance(pointer["managers"], dict), "service pointer schema differs")
    checked(pointer.get("product") == name and pointer.get("channel") == "stable", "service is not canonical stable")
    version = pointer.get("releaseVersion")
    checked(isinstance(version, str) and marker.channel(version) == "stable" and version == "v" + ".".join(str(part) for part in marker.order(version)[:3]), "service marker is not formal stable")
    resolved = source(f"PerishLab/{name}", version)
    checked(resolved["source"]["commit"] == pointer.get("commit"), "service tag differs from pointer")
    address = f"{authority}/v1/releases/stable/{version}/seal.json"
    checked(pointer["seal"].get("url") == address, "service pointer names another seal")
    seal = publication.verified(pointer["seal"], authority)
    fields = ("schema", "product", "releaseVersion", "channel", "commit", "generator", "managers", "artifacts", "provenance", "url")
    shape(seal, fields + (("guard",) if "guard" in seal else ()), "service seal")
    checked(seal["schema"] == 1 and seal["url"] == address and isinstance(seal["generator"], dict) and isinstance(seal["provenance"], dict) and isinstance(seal["artifacts"], dict) and isinstance(seal["managers"], dict), "service seal schema differs")
    checked(all(seal.get(key) == pointer.get(key) for key in ("product", "channel", "releaseVersion", "commit")), "service pointer and seal differ")
    record = document(f"{authority}/v1/releases/stable/{version}/distribution.json")
    distribution(record, resolved["source"])
    publication.material(seal["artifacts"][selection["placement"]["artifact"]], authority)
    return {"declaration": selection, "pointer": pointer, "seal": seal, "distribution": record, **resolved}


def services(specification, target):
    peers = declaration.services(specification)
    checked(target not in peers, "service repeats the published target")
    return {name: service(name, value) for name, value in sorted(peers.items())}


def combination(prepared, report):
    peers = prepared.get("services", {})
    products = report.get("products")
    target = prepared["admission"]["product"]
    checked(isinstance(products, list) and len(products) == len(peers) + 1 and all(isinstance(product, dict) for product in products), "prepared product set differs")
    names = [product.get("name") for product in products]
    checked(all(isinstance(name, str) for name in names) and len(set(names)) == len(names) and set(names) == {target, *peers}, "prepared product set is missing, duplicated or widened")
    for name, snapshot in peers.items():
        placement = snapshot["declaration"]["placement"]
        expected = {"name": name, "channel": "stable", "version": snapshot["pointer"]["releaseVersion"], "commit": snapshot["pointer"]["commit"], "seal": snapshot["pointer"]["seal"]["sha256"], "archive": snapshot["seal"]["artifacts"][placement["artifact"]]["sha256"], "placement": placement}
        checked(next(product for product in products if product["name"] == name) == expected, "consumed service package or placement differs")
