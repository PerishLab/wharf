import json
import subprocess
import tempfile
from pathlib import Path

from lib import parameters
from lib.content import canonical
from lib.process.promotion import assessment, control, material
from lib.refusal import Refusal
from lib.store import r2


def basis(held):
    observer, started = control.active(held["observer"])
    request = {name: held[name] for name in ("repository", "marker", "run")}
    request["attempt"] = held["planned"]
    return {"schema": material.SCHEMA, "observer": observer, "started": started, "request": request}


def produce(bucket, expected):
    result = {"schema": material.RESULT, **{name: expected[name] for name in ("observer", "started", "request")}, "promotion": False, "qualification": False, "association": False, "complete_publication": False}
    files = {}
    try:
        facts, files = assessment.prepare(bucket, expected["request"], expected["observer"])
        result.update(facts, state="prepared")
    except Refusal as failure:
        result.update(state="refused", reason=str(failure))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        result.update(state="unknown", reason="promotion preparation is unavailable or malformed")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for name, body in dict(files, **{"result.json": canonical.encode(result)}).items():
            (root / name).write_bytes(body)
        key = material.publish(bucket, expected, root)
    return result, key


def prepare(held):
    expected = basis(held)
    root = Path(held["source"]).resolve()
    root.mkdir(parents=True, exist_ok=False)
    result, key = produce(r2.configured(), expected)
    answer = dict(result, evidence={"key": key})
    (root / "preparation.json").write_bytes(canonical.encode(answer))
    parameters.answer({"digest": key, "state": result["state"]})
    return answer


def read(held):
    expected = basis(held)
    result, reference = material.restore(r2.configured(), held["digest"], expected)
    answer = dict(result, evidence=reference)
    (Path(held["source"]).resolve() / "readback.json").write_bytes(canonical.encode(answer))
    parameters.answer({"state": result["state"]})
    return answer
