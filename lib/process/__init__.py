import os
import subprocess
import sys

from lib.refusal import Refusal

REPOSITORY = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CONFIG",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    "GIT_DIR",
    "GIT_GRAFT_FILE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_NAMESPACE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PREFIX",
    "GIT_REPLACE_REF_BASE",
    "GIT_SHALLOW_FILE",
    "GIT_WORK_TREE",
)


def run(argv, cwd, env=None):
    return subprocess.run(argv, cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


def stream(argv, cwd, env=None):
    sys.stderr.flush()
    subprocess.run(argv, cwd=cwd, env=env, check=True, stdout=sys.stderr, stderr=subprocess.STDOUT)


def unanchored():
    return {name: value for name, value in os.environ.items() if name not in REPOSITORY}


def git(source, *args):
    result = subprocess.run(["git", "-C", str(source), *args], capture_output=True, text=True, env=unanchored())
    if result.returncode != 0:
        raise Refusal(f"git {' '.join(args)} failed in {source}: {result.stderr.strip()}")
    return result.stdout.strip()
