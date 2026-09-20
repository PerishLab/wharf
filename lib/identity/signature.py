from lib.process import run

SIGNER = "rcodesign"


def finalize(path, target, runner=run):
    if "apple-darwin" not in target:
        return None
    runner([SIGNER, "sign", str(path)], path.parent)
    return "adhoc"
