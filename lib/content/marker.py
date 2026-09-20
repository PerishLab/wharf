import re

from lib.refusal import Refusal

MARKER = re.compile(r"v(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)\.([1-9]\d*))?")
STAGES = {"alpha": 0, "beta": 1, "rc": 2}


def parts(value):
    matched = MARKER.fullmatch(value)
    if not matched:
        raise Refusal(f"marker {value!r} is not a release marker")
    return matched


def holds(value):
    return MARKER.fullmatch(value) is not None


def channel(value):
    return parts(value).group(4) or "stable"


def order(value):
    matched = parts(value)
    stage = (1, 0, 0) if matched.group(4) is None else (0, STAGES[matched.group(4)], int(matched.group(5)))
    return tuple(int(part) for part in matched.group(1, 2, 3)) + stage


def version(value):
    return parts(value).string.removeprefix("v")
