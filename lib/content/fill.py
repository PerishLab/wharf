from lib.refusal import Refusal


def named(text, index, values, out):
    if text[index:index + 1] == "{":
        out.append("{")
        return index + 1
    end = text.find("}", index)
    if end < 0:
        raise Refusal("a template holds an unclosed {")
    name = text[index:end]
    if not name:
        raise Refusal("a template holds an empty {}")
    if name not in values:
        raise Refusal(f"a template asks for {{{name}}}, which nothing names")
    out.append(values[name])
    return end + 1


def bare(text, index, out):
    if text[index:index + 1] != "}":
        raise Refusal("a template holds a } outside a variable")
    out.append("}")
    return index + 1


def fill(text, values):
    out = []
    index = 0
    while index < len(text):
        mark = text[index]
        if mark == "{":
            index = named(text, index + 1, values, out)
        elif mark == "}":
            index = bare(text, index + 1, out)
        else:
            out.append(mark)
            index += 1
    return "".join(out)
