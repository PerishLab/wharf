from lib.content import marker


def debian(held):
    matched = marker.parts(held)
    base = ".".join(matched.group(1, 2, 3))
    return base if matched.group(4) is None else f"{base}~{matched.group(4)}.{matched.group(5)}"
