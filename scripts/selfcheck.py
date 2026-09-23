import sys

from lib.check import actions, commands, files, imports, parameters, source, structure, vocabulary
from lib.process import git

CHECKS = (structure, source, imports, vocabulary, actions, commands, parameters)


def main():
    root = git(".", "rev-parse", "--show-toplevel")
    paths = files.listed(root)
    findings = [finding for check in CHECKS for finding in check.check(root, paths)]
    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
