import json
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from lib import resources
from lib.refusal import Refusal

KNOWN = resources.read_json("registries.json")["cargo"]


def declared(source, name):
    path = Path(source) / ".cargo/config.toml"
    config = tomllib.loads(path.read_text()) if path.is_file() else {}
    index = config.get("registries", {}).get(name, {}).get("index", "")
    location = index.removeprefix("sparse+")
    if KNOWN.get(name) != location:
        raise Refusal(f"cargo registry {name!r} at {location!r} is not a known distribution registry")
    return location


def entry(name):
    lowered = name.lower()
    if len(lowered) <= 2:
        return f"{len(lowered)}/{lowered}"
    if len(lowered) == 3:
        return f"3/{lowered[0]}/{lowered}"
    return f"{lowered[:2]}/{lowered[2:4]}/{lowered}"


def fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.read().decode()
    except urllib.error.HTTPError as failure:
        if failure.code == 404:
            return ""
        raise Refusal(f"cargo index {url} answered {failure.code}")


def published(location, package, version, reader=fetch):
    lines = reader(location + entry(package)).splitlines()
    return any(json.loads(line)["vers"] == version for line in lines if line.strip())
