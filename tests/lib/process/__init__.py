import base64
import hashlib


def evidence(**changed):
    return dict(
        {"attempts": 1, "status": 200, "request": None, "wait": None, "reset": 1700000000, "partial": False},
        **changed,
    )


def envelope(value, outcome="observed", **changed):
    return {"version": 1, "github": {"outcome": outcome, "evidence": evidence(**changed), "value": value}}


def error(outcome="refused", cause="http", status=404, **changed):
    return {"version": 1, "error": {"outcome": outcome, "cause": cause, "evidence": evidence(status=status, **changed)}}


def principal(login="PerishLab", **changed):
    return dict({"id": 330236468, "login": login, "node_id": "O_kgDOE68CNA", "type": "Organization"}, **changed)


def origin(name="PerishLab/wharf", identity=1374358737, **changed):
    return dict({"id": identity, "node_id": "R_kgDOUesM0Q", "full_name": name}, **changed)


def repository(name="PerishLab/wharf", identity=1374358737, **changed):
    return dict(
        {
            "id": identity,
            "node_id": "R_kgDOUesM0Q",
            "full_name": name,
            "private": False,
            "owner": principal(),
            "visibility": "public",
            "archived": False,
            "disabled": False,
            "default_branch": "main",
        },
        **changed,
    )


def branch(name="main", sha="1" * 40, repo="PerishLab/wharf", **changed):
    return dict(
        {"name": name, "protected": True, "commit": {"sha": sha, "url": f"https://api.github.com/repos/{repo}/commits/{sha}"}},
        **changed,
    )


def ref(kind="branch", name="main", sha="1" * 40, **changed):
    repo = changed.pop("repo", "PerishLab/wharf")
    prefix = "heads" if kind == "branch" else "tags"
    object_type = changed.pop("object_type", "commit" if kind == "branch" else "tag")
    segment = "commits" if object_type == "commit" else "tags"
    return dict(
        {
            "ref": f"refs/{prefix}/{name}",
            "url": f"https://api.github.com/repos/{repo}/git/refs/{prefix}/{name}",
            "object": {"sha": sha, "type": object_type, "url": f"https://api.github.com/repos/{repo}/git/{segment}/{sha}"},
        },
        **changed,
    )


def annotation(tag="v1.2.3", sha="e" * 40, target="b" * 40, **changed):
    repo = changed.pop("repo", "PerishLab/demo")
    return dict(
        {
            "sha": sha,
            "tag": tag,
            "url": f"https://api.github.com/repos/{repo}/git/tags/{sha}",
            "message": f"{tag}\n\npromotes {tag}-rc.1\n",
            "object": {"sha": target, "type": "commit", "url": f"https://api.github.com/repos/{repo}/git/commits/{target}"},
        },
        **changed,
    )


def commit(sha="1" * 40, tree="2" * 40, repo="PerishLab/wharf", **changed):
    return dict(
        {
            "sha": sha,
            "tree": {"sha": tree},
            "parents": [{"sha": "0" * 40}],
            "url": f"https://api.github.com/repos/{repo}/git/commits/{sha}",
        },
        **changed,
    )


def contents(path="plumb.toml", body=b"test", head="a" * 40, **changed):
    raw = body if isinstance(body, bytes) else body.encode("utf-8")
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    return dict(
        {
            "path": path,
            "commit": head,
            "sha": blob,
            "size": len(raw),
            "base64": base64.b64encode(raw).decode("ascii"),
        },
        **changed,
    )


def artifact(identity=1, head="a" * 40, data=b"archive", **changed):
    repo = changed.pop("repo", "PerishLab/example")
    run_id = changed.pop("run_id", identity * 10)
    repo_id = changed.pop("repository_id", 17)
    return dict(
        {
            "id": identity,
            "name": "guard-runtime",
            "size_in_bytes": len(data),
            "expired": False,
            "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
            "workflow_run": {"id": run_id, "repository_id": repo_id, "head_sha": head},
            "url": f"https://api.github.com/repos/{repo}/actions/artifacts/{identity}",
        },
        **changed,
    )


def artifacts(items, total=None, **changed):
    return dict({"total_count": len(items) if total is None else total, "artifacts": list(items)}, **changed)


def download(identity=1, data=b"archive", **changed):
    return dict({"id": identity, "size": len(data), "digest": "sha256:" + hashlib.sha256(data).hexdigest()}, **changed)


def trigger(name="follow-push", **changed):
    return dict({"event": name}, **changed)


def workflow(identity=7, head="a" * 40, status="completed", **changed):
    repo = origin()
    actor = principal("fixture-bot", id=101, node_id="U_fixture", type="User")
    return dict(
        {
            "id": identity,
            "workflow_id": 360475223,
            "run_attempt": 1,
            "head_sha": head,
            "head_branch": "main",
            "html_url": f"https://github.com/PerishLab/wharf/actions/runs/{identity}",
            "event": "workflow_dispatch",
            "display_title": "[ship] PerishLab/plumb v1.2.3-rc.1",
            "actor": actor,
            "triggering_actor": actor,
            "path": ".github/workflows/ship.yml",
            "status": status,
            "conclusion": None if status != "completed" else "success",
            "repository": dict(repo),
            "head_repository": dict(repo),
        },
        **changed,
    )


def step(name, number=1, status="completed", **changed):
    return dict({"number": number, "name": name, "status": status, "conclusion": None if status != "completed" else "success"}, **changed)


def job(name, run_id=20, identity=30, **changed):
    status = changed.pop("status", "in_progress")
    return dict(
        {
            "id": identity,
            "run_id": run_id,
            "run_attempt": 1,
            "head_sha": "a" * 40,
            "head_branch": "main",
            "html_url": f"https://github.com/PerishLab/wharf/actions/runs/{run_id}/job/{identity}",
            "name": name,
            "status": status,
            "conclusion": None if status != "completed" else "success",
            "started_at": "2026-10-30T10:00:00Z",
            "completed_at": None if status != "completed" else "2026-10-30T10:05:00Z",
            "steps": [],
        },
        **changed,
    )


def jobs(items, total=None, **changed):
    return dict({"total_count": len(items) if total is None else total, "jobs": list(items)}, **changed)


def ancestry(base="1" * 40, head="2" * 40, status="ahead", **changed):
    repo = changed.pop("repo", "PerishLab/wharf")
    merge = changed.pop("merge", base)
    return dict(
        {
            "url": f"https://api.github.com/repos/{repo}/compare/{base}...{head}",
            "base_commit": {"sha": base},
            "merge_base_commit": {"sha": merge},
            "status": status,
        },
        **changed,
    )
