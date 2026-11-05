from lib.content import marker


def version(held):
    matched = marker.parts(held)
    base = ".".join(matched.group(1, 2, 3))
    suffix = "" if matched.group(4) is None else f"_{matched.group(4)}{matched.group(5)}"
    return f"{base}{suffix}-r0"
