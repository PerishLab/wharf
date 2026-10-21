import os
import tempfile
from pathlib import Path

from lib.cargo import toolchain
from lib.content import marker, resources
from lib.content.static import evidence
from lib.identity.guard import snapshot
from lib.refusal import Refusal

WINDOWS = os.name == "nt"
MACHINE = ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "SystemDrive", "VCINSTALLDIR", "VSINSTALLDIR", "VCToolsInstallDir", "WindowsSdkDir", "WindowsSDKVersion", "UniversalCRTSdkDir", "UCRTVersion", "INCLUDE", "LIB", "LIBPATH")


def case(source, version, selection, domain):
    if not WINDOWS or not marker.holds(version):
        raise Refusal("native staging verification needs Windows and an exact release marker")
    declared = resources.read_json("manager/staging.json")
    if selection not in declared["cases"]:
        raise Refusal("native staging verification names an unsupported case")
    source = Path(source).resolve()
    versions = toolchain.checked(domain)
    with tempfile.TemporaryDirectory(prefix="wharf-staging-") as home:
        env = evidence.clean(os.environ, home)
        env.update({"USERPROFILE": home, "CARGO_HOME": os.environ.get("CARGO_HOME", str(Path.home() / ".cargo")), "RUSTUP_HOME": os.environ.get("RUSTUP_HOME", str(Path.home() / ".rustup")), "RUSTUP_TOOLCHAIN": versions["rust.version"]})
        env.update({name: os.environ[name] for name in MACHINE if name in os.environ})
        env.update({"TEMP": home, "TMP": home})
        path = env["PATH"]
        tools = evidence.tools(source, env, evidence.inspect, ("git",))
        snapshot.rust(source, (tools, env, path), evidence.inspect)
        env["PATH"] = os.pathsep.join(dict.fromkeys(env["PATH"].split(os.pathsep) + path.split(os.pathsep)))
        if tools["rustc"]["version"].split()[1] != versions["rust.version"]:
            raise Refusal("native staging verification requires the domain Rust")
        held = {"root": source, "env": env, "tools": tools, "inspect": evidence.inspect}
        origin = snapshot.git(held, "remote", "get-url", "origin")
        if origin not in {f"https://github.com/{declared['repository']}", f"https://github.com/{declared['repository']}.git"}:
            raise Refusal("native staging verification refuses a foreign repository")
        head = snapshot.git(held, "rev-parse", "HEAD")
        if snapshot.git(held, "rev-parse", f"refs/tags/{version}^{{commit}}") != head:
            raise Refusal("native staging verification checkout differs from its exact marker")
        tree = snapshot.git(held, "rev-parse", "HEAD^{tree}")
        snapshot.clean(held)
        spec = declared["cases"][selection]
        argv = [tools["cargo"]["path"], "test", "--locked", "--features", declared["features"], "-p", "plumb", *spec["target"], spec["filter"], "--", "--nocapture"]
        output = evidence.inspect(argv, source, env)
        if not all(f"test {name} ... ok" in output for name in spec["expected"]):
            raise Refusal("native staging verification did not execute every declared regression")
        snapshot.unchanged(held, head, tree)
        return {"action": selection, "repository": declared["repository"], "marker": version, "commit": head, "tree": tree, "cases": spec["expected"], "domain": versions, "tools": tools}
