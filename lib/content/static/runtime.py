import hashlib
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from lib.content import implementation, resources
from lib.content.static import evidence
from lib.refusal import Refusal

POLICY = resources.read_json("build.json")["preview"]
DAEMON = "unix:///var/run/docker.sock"
LABEL = "perish.wharf.preview.owner"
HEX = re.compile(r"[0-9a-f]{64}")


class Uncertain(Refusal):
    pass


@dataclass(frozen=True)
class Guest:
    source: Path
    controls: Path
    command: list
    environment: dict


@dataclass(frozen=True)
class Execution:
    stdout: str
    runtime: dict


def directory(value):
    path = Path(value)
    temporary = Path(tempfile.gettempdir()).resolve()
    if not path.is_absolute() or not path.is_dir() or path == temporary or not path.is_relative_to(temporary):
        raise Refusal("Preview guest mounts must be selected disposable directories under the temporary root")
    if path.resolve() != path or any(value in str(path) for value in (",", "\n", "\r")) or any(part.is_symlink() for part in (path, *path.parents)):
        raise Refusal("Preview guest mount ancestors must not be symlinks")
    return path


def qualified(guest):
    source, controls = directory(guest.source), directory(guest.controls)
    if source.is_relative_to(controls) or controls.is_relative_to(source):
        raise Refusal("Preview product and trusted controls must be separate directories")
    if not isinstance(guest.command, list) or not guest.command or any(not isinstance(value, str) or not value or "\x00" in value for value in guest.command):
        raise Refusal("Preview guest command must be explicit argument data")
    allowed = {"CI": "true", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PLUMB_GUARD_STRENGTH": "full", "PLUMB_GUARD_BOUNDARY": "head",
               "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1",
               "NPM_CONFIG_IGNORE_SCRIPTS": "true", "NPM_CONFIG_IGNORE_PNPMFILE": "true"}
    if not isinstance(guest.environment, dict) or any(key not in allowed or value != allowed[key] for key, value in guest.environment.items()):
        raise Refusal("Preview guest environment must contain only fixed public execution selectors")
    return source, controls


def profile(guest, owner, world):
    source, controls = qualified(guest)
    return ["container", "create", "--name", "wharf-preview-" + owner, "--label", LABEL + "=" + owner,
            "--pull", "never", "--platform", POLICY["platform"], "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--network", "none", "--no-healthcheck", "--tty=false", "--interactive=false", "--user", "1000:1000",
            "--pids-limit", str(POLICY["pids"]), "--memory", str(POLICY["memory"]), "--memory-swap", str(POLICY["memory"]),
            "--cpus", str(POLICY["cpus"]), "--tmpfs", f'/tmp:rw,nosuid,nodev,size={POLICY["temporary"]}', "--workdir", "/source",
            "--mount", f"type=bind,src={source},dst=/source", "--mount", f"type=bind,src={controls},dst=/controls,readonly",
            "--entrypoint", "/usr/bin/env", world["image"]["resolved"], "-i", "PATH=" + POLICY["path"], "HOME=/tmp",
            *[f"{key}={value}" for key, value in sorted(guest.environment.items())], *guest.command]


def stopped(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()
    process.stdout.close()
    process.stderr.close()


def attach(argv, environment, timeout):
    process = subprocess.Popen(argv, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    body = bytearray()
    streams = {process.stdout, process.stderr}
    total = 0
    deadline = time.monotonic() + timeout
    logging = uuid.uuid4().hex
    print("::stop-commands::" + logging, file=sys.stderr, flush=True)
    try:
        while streams:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Refusal("Preview guest command exceeded its execution budget")
            for stream in select.select(list(streams), [], [], remaining)[0]:
                chunk = os.read(stream.fileno(), 32768)
                if not chunk:
                    streams.remove(stream)
                    continue
                total += len(chunk)
                if total > evidence.LIMIT:
                    raise Refusal("Preview guest command exceeded its combined 1 MiB output budget")
                if stream is process.stdout:
                    body.extend(chunk)
                sys.stderr.write(chunk.decode("utf-8", errors="replace"))
                sys.stderr.flush()
        code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        if code != 0:
            raise Refusal(f"Preview guest command exited {code}")
        return body.decode("utf-8")
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as error:
        raise Refusal("Preview guest command failed or exceeded its execution budget") from error
    finally:
        stopped(process)
        print("\n::" + logging + "::", file=sys.stderr, flush=True)


class Engine:
    def __init__(self, home):
        found = shutil.which("docker", path="/usr/local/bin:/usr/bin:/bin")
        if found is None or os.name != "posix":
            raise Refusal("Preview guest execution requires the prepared local POSIX Docker runner")
        self.path = str(Path(found).resolve())
        self.environment = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(home)}
        self.config = str(Path(home) / "docker")

    def __call__(self, arguments, streaming=False):
        argv = [self.path, "--host", DAEMON, "--config", self.config, *arguments]
        if streaming:
            return attach(argv, self.environment, POLICY["timeout"])
        try:
            result = subprocess.run(argv, env=self.environment, capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise Refusal("Preview engine operation failed or exceeded its control budget") from error
        if max(len(result.stdout), len(result.stderr)) > evidence.LIMIT:
            raise Refusal("Preview engine exceeded its control output budget")
        if result.returncode != 0:
            detail = result.stderr[:8192].decode("utf-8", errors="replace").strip()
            raise Refusal(f"Preview engine operation {arguments[:2]} exited {result.returncode}: {detail}")
        try:
            return result.stdout.decode("utf-8")
        except UnicodeError as error:
            raise Refusal("Preview engine returned invalid UTF-8") from error

    def prepare(self):
        self(["image", "pull", "--platform", POLICY["platform"], POLICY["image"]], True)

    def world(self):
        image = evidence.decode(self(["image", "inspect", POLICY["image"]]))
        server = evidence.decode(self(["info", "--format", "{{json .}}"] ))
        if not isinstance(image, list) or len(image) != 1 or POLICY["image"] not in (image[0].get("RepoTags") or []) or image[0].get("Os") != "linux" or image[0].get("Architecture") != "amd64":
            raise Refusal("Preview execution image does not match its trusted immutable identity")
        repository = POLICY["image"].removesuffix(":stable")
        digests = [value for value in image[0].get("RepoDigests", []) if isinstance(value, str) and value.startswith(repository + "@sha256:") and HEX.fullmatch(value.removeprefix(repository + "@sha256:"))]
        if len(digests) != 1:
            raise Refusal("Preview stable image must resolve to one exact public repository digest")
        if not isinstance(server, dict) or server.get("OSType") != "linux" or not any("seccomp" in value and "unconfined" not in value for value in server.get("SecurityOptions", [])):
            raise Refusal("Preview engine must provide the fixed Linux seccomp execution boundary")
        return {"image": {"requested": POLICY["image"], "resolved": digests[0], "id": image[0]["Id"]}, "profile": POLICY,
                "engine": {key: server.get(key) for key in ("ServerVersion", "KernelVersion", "Architecture", "SecurityOptions")},
                "client": {"path": self.path, "sha256": hashlib.sha256(Path(self.path).read_bytes()).hexdigest()},
                "implementation": implementation.resourced(["lib.content.static.runtime"], ["build.json"])}


def observed(engine, identity):
    held = evidence.decode(engine(["container", "inspect", identity]))
    if not isinstance(held, list) or len(held) != 1 or not isinstance(held[0], dict) or held[0].get("Id") != identity:
        raise Refusal("Preview engine did not identify the exact owned container")
    return held[0]


def matches(held, owner):
    if held.get("Name") != "/wharf-preview-" + owner or held.get("Config", {}).get("Labels", {}).get(LABEL) != owner:
        raise Refusal("Preview cleanup refuses a container outside this exact invocation")


def owned(engine, owner):
    body = engine(["container", "ls", "--all", "--filter", "name=^/wharf-preview-" + owner + "$", "--filter", "label=" + LABEL + "=" + owner, "--no-trunc", "--format", "{{.ID}}"])
    identities = body.splitlines()
    if len(identities) > 1 or any(not HEX.fullmatch(value) for value in identities):
        raise Refusal("Preview owned-container readback is ambiguous")
    return identities


def teardown(engine, owner):
    identities = owned(engine, owner)
    for identity in identities:
        matches(observed(engine, identity), owner)
        engine(["container", "rm", "--force", identity])
    if owned(engine, owner):
        raise Refusal("Preview guest absence was not independently confirmed")


def checked(held, guest, world):
    host, config = held.get("HostConfig", {}), held.get("Config", {})
    if config.get("Tty") is not False or config.get("OpenStdin") is not False:
        raise Refusal("Preview result transport requires separate non-interactive streams without a TTY")
    fixed = {"ReadonlyRootfs": True, "Privileged": False, "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges"],
             "NetworkMode": "none", "PidsLimit": POLICY["pids"], "Memory": POLICY["memory"], "MemorySwap": POLICY["memory"], "NanoCpus": POLICY["cpus"] * 1000000000}
    if any(host.get(key) != value for key, value in fixed.items()) or any(host.get(key) for key in ("Binds", "Devices", "CapAdd", "PidMode", "PortBindings")) or host.get("IpcMode") == "host":
        raise Refusal("Preview guest effective restrictions disagree with the fixed profile")
    actual = {(entry.get("Source"), entry.get("Destination"), entry.get("RW"), entry.get("Type")) for entry in held.get("Mounts", [])}
    wanted = {(str(guest.source), "/source", True, "bind"), (str(guest.controls), "/controls", False, "bind")}
    expected = profile(guest, "0" * 32, world)
    command = expected[expected.index(world["image"]["resolved"]) + 1:]
    temporary = {"/tmp": f'rw,nosuid,nodev,size={POLICY["temporary"]}'}
    if actual != wanted or len(held.get("Mounts", [])) != 2 or config.get("User") != "1000:1000" or config.get("Entrypoint") != ["/usr/bin/env"] or held.get("Image") != world["image"]["id"] or config.get("Cmd") != command or host.get("Tmpfs") != temporary:
        raise Refusal("Preview guest mounts, entrypoint, user or image disagree with the trusted profile")


def run(guest, engine=None):
    qualified(guest)
    with tempfile.TemporaryDirectory(prefix="wharf-preview-engine-") as home:
        engine = engine or Engine(home)
        engine.prepare()
        world = engine.world()
        owner = uuid.uuid4().hex
        try:
            identity = engine(profile(guest, owner, world)).strip()
            if not HEX.fullmatch(identity):
                raise Refusal("Preview creation did not return one exact container identity")
            held = observed(engine, identity)
            matches(held, owner)
            checked(held, guest, world)
            body = engine(["container", "start", "--attach", identity], True)
            state = observed(engine, identity).get("State", {})
            if state.get("Status") != "exited" or state.get("Running") is not False or state.get("ExitCode") != 0 or state.get("OOMKilled") is not False:
                raise Refusal("Preview command completion was not independently confirmed")
        except Exception as error:
            raise Refusal(f"Preview execution refused; invocation {owner} requires confirmed teardown") from error
        finally:
            try:
                teardown(engine, owner)
            except Exception as error:
                raise Uncertain(f"Preview teardown unknown for invocation {owner}; no output is admissible") from error
        if engine.world() != world:
            raise Refusal("Preview execution world changed; no output is admissible")
        return Execution(body, world)
