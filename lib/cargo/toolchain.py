import re

from lib.cargo.manifest import read, tracked
from lib.refusal import Refusal

PATH = "rust-toolchain.toml"


def declared(source):
    if not tracked(source, PATH):
        raise Refusal(f"the product must declare its Rust toolchain in {PATH}")
    toolchain = read(source, PATH).get("toolchain", {})
    channel = toolchain.get("channel", "")
    if not re.fullmatch(r"\d+\.\d+\.\d+", channel):
        raise Refusal(f"{PATH} channel {channel!r} must be an exact x.y.z version")
    return {
        "channel": channel,
        "profile": toolchain.get("profile", "default"),
        "components": sorted(toolchain.get("components", [])),
        "targets": sorted(toolchain.get("targets", [])),
    }
