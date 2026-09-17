from pathlib import PurePosixPath

from lib.process import git


def listed(root):
    output = git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return sorted({PurePosixPath(name) for name in output.split("\0") if name})
