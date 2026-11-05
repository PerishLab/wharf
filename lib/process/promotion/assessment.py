import json

from lib.content import canonical
from lib.identity.bind import candidate
from lib.identity.bind.spec import entry, require
from lib.media import release
from lib.process.promotion import reader, subject


def matched(qualified, association, evidence):
    report = qualified["report"]
    reference = evidence["reference"]["proof"]
    require(report["evidence"]["source"] == reference["source"], "qualification source differs from associated content")
    ref = report["evidence"]["credential"]
    _, authority, _ = candidate.control.location(reference["source"]["repository"], reference["source"]["marker"])
    require(ref["url"].startswith(authority + "/v1/releases/") and 0 < ref["size"] <= reader.LIMIT, "qualification credential reference is foreign or unbounded")
    body = release.fetch(ref["url"])
    require(entry(body) == {name: ref[name] for name in ("size", "sha256")}, "qualified credential bytes moved")
    passed = json.loads(body)
    require(passed["source"] == reference["source"] and passed["declaration"]["manifest"] == association["declaration"]["manifest"], "qualification declaration differs from candidate")
    products = passed["products"]
    require(isinstance(products, list) and len(products) == 1, "promotion pilot has an unsupported product combination")
    product = products[0]
    require(product["name"] == reference["source"]["repository"].split("/")[1] and product["version"] == reference["source"]["marker"] and product["commit"] == reference["source"]["commit"], "qualified product identity differs")
    require(product["archive"] == reference["archive"]["sha256"] and product["seal"] == reference["seal"]["sha256"], "qualification tested another archive or seal")
    return passed


def assess(bucket, request):
    evidence = {}
    association, archive = candidate.assess(bucket, request, evidence)
    qualified = reader.check(request["repository"], request["reference"])
    passed = matched(qualified, association, evidence)
    return {"association.json": canonical.encode(association), "verification.json": canonical.encode(evidence), "candidate.tar.gz": archive, "qualification.json": canonical.encode(qualified), "credential.json": canonical.encode(passed)}


def prepare(bucket, request, observer):
    document, selected, identity = subject.resolve(bucket, request, observer)
    files = assess(bucket, selected)
    subject.unchanged(identity)
    latest = reader.check(selected["repository"], selected["reference"])
    before = json.loads(files["qualification.json"])["report"]["evidence"]
    after = latest["report"]["evidence"]
    require(all(before[name] == after[name] for name in ("credential", "intent", "source", "tag", "observer", "world")) and before["final_requirements"]["requirements"] == after["final_requirements"]["requirements"], "qualification changed during candidate collection")
    files["qualification.json"] = canonical.encode(latest)
    result = {"identity": identity, "plan": canonical.digest(document), "qualification": True, "association": True, "scope": {"target": "x86_64-unknown-linux-gnu", "medium": "installation-tar.gz"}, "complete_publication": False, "unassessed": ["linux-arm64-musl", "apk", "macos", "windows", "oci", "cargo", "npm", "chart", "cfworker", "deb", "cross-product"]}
    return result, files
