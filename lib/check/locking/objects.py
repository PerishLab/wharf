import hashlib
import os
import re
import select
import signal
import subprocess
import time

from lib.check.locking.literal import refuse

TIMEOUT = 30
INVENTORY = 2 * 1024 * 1024
ENTRIES = 20000
HEX = re.compile(r"[0-9a-f]{40}")


def command(root, arguments, env, limit):
    try:
        process = subprocess.Popen(["/usr/bin/git", "-C", str(root), *arguments], env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        refuse()
    body = bytearray()
    deadline = time.monotonic() + TIMEOUT
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                refuse()
            chunk = os.read(process.stdout.fileno(), 32768)
            if not chunk:
                break
            if len(body) + len(chunk) > limit:
                refuse()
            body.extend(chunk)
        if process.wait(timeout=max(0.001, deadline - time.monotonic())) != 0:
            refuse()
        return bytes(body)
    except (OSError, subprocess.TimeoutExpired):
        refuse()
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()
        process.stdout.close()


def inventory(body):
    if len(body) > INVENTORY or not body.endswith(b"\0"):
        refuse()
    entries = body[:-1].split(b"\0")
    if len(entries) > ENTRIES:
        refuse()
    result = {}
    for entry in entries:
        try:
            metadata, path = entry.split(b"\t", 1)
            mode, kind, identity = metadata.decode("ascii").split(" ")
            name = path.decode("utf-8")
        except (ValueError, UnicodeError):
            refuse()
        if mode not in {"100644", "100755", "120000"} or kind != "blob" or not HEX.fullmatch(identity) or not name or name in result:
            refuse()
        result[name] = (mode, identity)
    return result


def blob(root, entry, env, limit):
    mode, identity = entry
    if mode not in {"100644", "100755"} or not HEX.fullmatch(identity):
        refuse()
    body = command(root, ["cat-file", "blob", identity], env, limit)
    header = f"blob {len(body)}\0".encode()
    if hashlib.sha1(header + body).hexdigest() != identity:
        refuse()
    return body
