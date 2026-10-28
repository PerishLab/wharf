import contextlib
import multiprocessing
import subprocess
import sys
import time
from dataclasses import dataclass

from lib.process.job import Job, attach
from lib.refusal import Refusal


@dataclass(frozen=True)
class Request:
    argv: object
    cwd: object
    env: object


def worker(connection, request, name):
    try:
        attach(name)
        with subprocess.Popen(request.argv, cwd=request.cwd, env=request.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as process:
            for line in process.stdout:
                connection.send(("line", line))
            code = process.wait()
        connection.send(("done", code))
    except Exception as failure:
        with contextlib.suppress(BrokenPipeError, EOFError):
            connection.send(("error", f"{type(failure).__name__}: {failure}"))
    finally:
        connection.close()


def observe(connection, peer, timeout):
    lines = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not connection.poll(min(0.05, max(0, deadline - time.monotonic()))):
            if not peer.is_alive():
                raise Refusal("the native suite supervisor ended without its result")
            continue
        try:
            kind, value = connection.recv()
        except EOFError as failure:
            raise Refusal("the native suite supervisor closed without its result") from failure
        if kind == "line":
            lines.append(value)
            sys.stderr.write(value)
            sys.stderr.flush()
        elif kind == "done":
            return value, "".join(lines)
        else:
            raise Refusal(f"the native suite supervisor refused: {value}")
    raise Refusal(f"the test suite exceeded {timeout}s")


def attempt(argv, cwd, env, timeout):
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    peer = None
    sys.stderr.flush()
    try:
        with Job() as job:
            peer = context.Process(target=worker, args=(sender, Request(argv, cwd, env), job.name))
            peer.start()
            sender.close()
            result = observe(receiver, peer, timeout)
            peer.join(5)
            if peer.is_alive() or peer.exitcode != 0:
                raise Refusal("the native suite supervisor did not finish cleanly")
        return result
    except OSError as failure:
        raise Refusal("the native suite process boundary failed") from failure
    finally:
        receiver.close()
        sender.close()
        if peer is not None and peer.pid is not None:
            peer.join(5)
            if peer.is_alive():
                peer.kill()
                peer.join(5)
            if peer.is_alive():
                raise Refusal("the native suite supervisor could not be reaped")
            peer.close()
