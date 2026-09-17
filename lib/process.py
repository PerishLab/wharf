import subprocess

from lib.refusal import Refusal


def run(argv, cwd, env=None):
    return subprocess.run(argv, cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


def git(source, *args):
    result = subprocess.run(["git", "-C", str(source), *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise Refusal(f"git {' '.join(args)} failed in {source}: {result.stderr.strip()}")
    return result.stdout.strip()
