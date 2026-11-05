import hashlib
import re
import tempfile
from pathlib import Path

from lib.content import marker, resources
from lib.package.openwrt import package
from lib.package.openwrt.version import version
from lib.process import run
from lib.refusal import Refusal

LAYOUT = resources.read_json("releases.json")["placements"]


def entry(seal, kind):
    return seal.get("artifacts", {}).get(LAYOUT[kind]["key"])


def content(bucket, authority, held):
    prefix = f"{authority}/v1/objects/sha256/"
    if not held["url"].startswith(prefix):
        raise Refusal("OpenWrt feed object is outside its release authority")
    key = held["url"].removeprefix(authority + "/")
    raw = bucket.get(key)
    if hashlib.sha256(raw).hexdigest() != held["sha256"] or len(raw) != held["size"]:
        raise Refusal("OpenWrt feed object differs from its immutable release seal")
    return raw


def indexed(raw, public, runner):
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        (root / "keys").mkdir()
        (root / "keys/signing.pem").write_bytes(public)
        (root / "packages.adb").write_bytes(raw)
        argv = package.container(package.BUILDER, [(root, "/work", True)])
        runner([*argv, "apk", "--keys-dir", "/work/keys", "verify", "/work/packages.adb"], ".")
        dumped = runner([*argv, "apk", "adbdump", "/work/packages.adb"], ".")
        versions = re.findall(r"^    version: (\S+)$", dumped, re.MULTILINE)
        if len(versions) != 1:
            raise Refusal("OpenWrt feed must contain exactly one package version")
        return versions[0]


def point(bucket, authority, seal, reader):
    index = entry(seal, "apk-index")
    if index is None:
        return None
    public = content(bucket, authority, entry(seal, "apk-key"))
    raw = content(bucket, authority, index)
    current_version = indexed(raw, public, run)
    if current_version != version(seal["releaseVersion"]):
        raise Refusal("OpenWrt feed package version differs from its release marker")
    apk = entry(seal, "apk")
    if reader(apk["url"]) != content(bucket, authority, apk) or reader(index["url"]) != raw or reader(entry(seal, "apk-key")["url"]) != public:
        raise Refusal("OpenWrt feed package and index must read back publicly before activation")
    key = f"v1/apk/channels/{marker.channel(seal['releaseVersion'])}/{package.PROFILE['architecture']}/packages.adb"
    previous, etag = bucket.snapshot(key)
    if previous is not None:
        standing = indexed(previous, public, run)
        argv = package.container(package.BUILDER, [])
        order = run([*argv, "apk", "version", "-t", standing, current_version], ".").strip()
        if order not in ("<", "=", ">"):
            raise Refusal("OpenWrt feed version ordering is unknown")
        if order == ">":
            raise Refusal("a newer OpenWrt feed already exists; rediscover the current release channel")
        if order == "=":
            if order == "=" and previous != raw:
                raise Refusal("OpenWrt feed already holds different bytes for this package version")
            if reader(f"{authority}/{key}") != previous:
                raise Refusal("standing OpenWrt channel feed does not read back publicly")
            return {"feed": key, "state": "kept", "version": standing}
    bucket.swap(key, raw, etag)
    if reader(f"{authority}/{key}") != raw:
        raise Refusal("OpenWrt channel feed did not read back after activation")
    return {"feed": key, "state": "advanced", "version": current_version}
