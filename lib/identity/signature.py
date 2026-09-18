import sys

from lib.process import run
from lib.refusal import Refusal

STEPS = (["--force", "--sign", "-", "--timestamp=none"], ["--verify", "--strict"])


def finalize(path, target, runner=run):
    if "apple-darwin" not in target:
        return None
    if sys.platform != "darwin":
        raise Refusal("Mach-O identity finalization requires its native macOS runner")
    for step in STEPS:
        runner(["codesign", *step, str(path)], path.parent)
    return "adhoc"
