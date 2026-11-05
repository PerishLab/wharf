import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from lib.package.openwrt import package
from lib.process import run
from lib.refusal import Refusal
from lib.store.r2 import secret

VARIABLE = "WHARF_APK_SIGNING_KEY"


def generated(directory, runner=run):
    directory = Path(directory)
    private = directory / "signing.pem"
    runner(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(private)], ".")
    private.chmod(0o600)
    return private


def public(private, output, runner=run):
    runner(["openssl", "pkey", "-in", str(private), "-pubout", "-out", str(output)], ".")
    return Path(output)


def signed(source, private, output, runner=run):
    output = Path(output)
    output.mkdir(parents=True)
    target = output / Path(source).name
    shutil.copyfile(source, target)
    keys = output / "keys"
    keys.mkdir()
    public(private, keys / "signing.pem", runner)
    argv = package.container(package.BUILDER, [(output, "/work", False), (private, "/signing.pem", True)])
    original = runner([*argv, "apk", "adbdump", f"/work/{target.name}"], ".")
    runner([*argv, "apk", "--allow-untrusted", "adbsign", "--sign-key", "/signing.pem", f"/work/{target.name}"], ".")
    runner([*argv, "apk", "--keys-dir", "/work/keys", "verify", f"/work/{target.name}"], ".")
    final = runner([*argv, "apk", "adbdump", f"/work/{target.name}"], ".")
    stripped = lambda text: [line for line in text.splitlines() if not line.startswith("# sig ")]
    if stripped(original) != stripped(final):
        raise Refusal("APK signing changed package metadata or payload identities")
    return target


def placed(source, authority, output, runner=run):
    value = secret(os.environ, VARIABLE)
    if not value or len(value) > 16384:
        raise Refusal(f"OpenWrt APK publication requires {VARIABLE} or its _FILE seat")
    with tempfile.TemporaryDirectory() as scratch:
        private = Path(scratch) / "signing.pem"
        private.write_text(value + "\n")
        private.chmod(0o600)
        target = signed(source, private, output, runner)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        spec = f"{authority}/v1/objects/sha256/{digest}/${{name}}-${{version}}.apk"
        argv = package.container(package.BUILDER, [(output, "/work", False), (private, "/signing.pem", True)])
        runner([*argv, "apk", "--keys-dir", "/work/keys", "mkndx", "--compression", "deflate:9", "--sign-key", "/signing.pem", "--pkgname-spec", spec, "--output", "/work/packages.adb", f"/work/{target.name}"], ".")
        runner([*argv, "apk", "--keys-dir", "/work/keys", "verify", "/work/packages.adb"], ".")
    return {"apk": target, "apk-index": Path(output) / "packages.adb", "apk-key": Path(output) / "keys/signing.pem"}
