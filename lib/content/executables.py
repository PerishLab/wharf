import re
from dataclasses import dataclass

from lib.content import declaration
from lib.refusal import Refusal

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
FIELDS = ("targets", "install")


@dataclass(frozen=True)
class Executable:
    name: str
    targets: tuple
    install: bool


def listed(held):
    names = held.get("binaries", [])
    if not isinstance(names, list) or not all(isinstance(name, str) and NAME.fullmatch(name) for name in names):
        raise Refusal("[release].binaries must list executable names")
    if len(set(names)) != len(names):
        raise Refusal("[release].binaries names an executable twice")
    return names


def tabled(held, names):
    tables = held.get("binary", {})
    if not isinstance(tables, dict) or not all(isinstance(table, dict) for table in tables.values()):
        raise Refusal("[release.binary] must hold one table per executable")
    unknown = sorted(set(tables) - set(names))
    if unknown:
        raise Refusal(f"[release.binary] configures {', '.join(unknown)}, which [release].binaries does not list")
    for name, table in tables.items():
        extra = sorted(set(table) - set(FIELDS))
        if extra:
            raise Refusal(f"[release.binary.{name}] holds {', '.join(extra)}; it takes only {', '.join(FIELDS)}")
    return tables


def described(name, table, union):
    targets = table.get("targets", union)
    if not isinstance(targets, list) or not targets or not all(isinstance(target, str) for target in targets):
        raise Refusal(f"[release.binary.{name}].targets must list at least one target")
    outside = [target for target in targets if target not in union]
    if outside:
        raise Refusal(f"{name} declares targets [release].targets does not: {', '.join(outside)}")
    install = table.get("install", True)
    if not isinstance(install, bool):
        raise Refusal(f"[release.binary.{name}].install must be true or false")
    return Executable(name, tuple(target for target in union if target in targets), install)


def declared(source, union):
    held = declaration.release(source)
    names = listed(held)
    tables = tabled(held, names)
    return [described(name, tables.get(name, {}), list(union)) for name in names]


def primary(executables, product):
    names = [executable.name for executable in executables]
    if not names:
        raise Refusal("the product declares no executable")
    return product if product in names else names[0]


def built(executables, target):
    return [executable.name for executable in executables if target in executable.targets]


def installed(executables, target):
    return [executable.name for executable in executables if executable.install and target in executable.targets]


def placed(source, kind, executables, product):
    table = declaration.release(source).get(kind)
    if table is None:
        return None
    name = table.get("binary", primary(executables, product)) if isinstance(table, dict) else None
    if name not in [executable.name for executable in executables]:
        raise Refusal(f"[release.{kind}] names the executable {name!r}, which [release].binaries does not list")
    return name
