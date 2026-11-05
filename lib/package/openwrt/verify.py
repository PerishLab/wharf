import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from lib.content import resources
from lib.package.openwrt import package, signing
from lib.process import run
from lib.refusal import Refusal

IMAGE = resources.read_json("build/config.json")["apk"]["verifier"]


@dataclass(frozen=True)
class Check:
    apk: Path
    binary: str
    marker: str
    digest: str
    version: str
    depends: tuple = ()


def installed(check, signed, runner):
    network = [] if check.depends else ["--network", "none"]
    container = runner(["docker", "run", "-d", *network, "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--entrypoint", "/bin/sleep", "--mount", f"type=bind,src={signed.parent.resolve()},dst=/package,readonly", IMAGE, "600"], ".").strip()
    try:
        runner(["docker", "cp", str(signed.parent / "architectures"), f"{container}:/etc/apk/arch"], ".")
        prefix = ["docker", "exec", container]
        if check.depends:
            runner([*prefix, "apk", "update"], ".")
            runner([*prefix, "apk", "add", *check.depends], ".")
        runner([*prefix, "apk", "--keys-dir", "/package/keys", "verify", f"/package/{signed.name}"], ".")
        runner([*prefix, "apk", "--keys-dir", "/package/keys", "--repositories-file", "/dev/null", "--no-network", "add", f"/package/{signed.name}"], ".")
        listed = runner([*prefix, "apk", "info", "-v"], ".").splitlines()
        if f"{check.binary}-{check.version}" not in listed:
            raise Refusal("OpenWrt installed package reports a different package version")
        digest = runner([*prefix, "sha256sum", f"/usr/bin/{check.binary}"], ".").split()[0]
        if digest != check.digest:
            raise Refusal("OpenWrt installed binary differs from the verified bound executable")
        reported = runner([*prefix, f"/usr/bin/{check.binary}", "--version"], ".").strip()
        if reported != f"{check.binary} {check.marker}":
            raise Refusal("OpenWrt installed binary reports a different release identity")
        runner([*prefix, "apk", "--repositories-file", "/dev/null", "--no-network", "del", check.binary], ".")
        runner([*prefix, "test", "!", "-e", f"/usr/bin/{check.binary}"], ".")
        return reported
    finally:
        runner(["docker", "rm", "-f", container], ".")


def verify(check, output, runner=run):
    output = Path(output)
    if output.exists():
        raise Refusal(f"output {output} already exists")
    with tempfile.TemporaryDirectory() as scratch:
        directory = Path(scratch)
        private = signing.generated(directory, runner)
        signed = signing.signed(check.apk, private, directory / "signed", runner)
        (signed.parent / "architectures").write_text("aarch64_generic\naarch64_cortex-a53\n")
        reported = installed(check, signed, runner)
    output.mkdir(parents=True)
    receipt = {"action": "ship.apk.verify", "image": IMAGE, "package": check.binary, "version": check.version, "reported": reported, "binary": check.digest, "apk": hashlib.sha256(Path(check.apk).read_bytes()).hexdigest(), "native": "aarch64", "architectures": ["aarch64_generic", package.PROFILE["architecture"]], "depends": list(check.depends), "removed": True}
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return receipt
