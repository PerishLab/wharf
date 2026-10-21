from lib.content.static import admission
from lib.content.static.github import Routing
from lib.process.confirmation import history, original
from lib.process.lane import entry, reservation
from lib.refusal import Refusal


def observer(pins, context, api):
    if context != pins["observer"]["context"]:
        raise Refusal("lane confirmation current platform context differs from independent observer pins")
    run, workflow = admission.workflow(api, context, entry.policy())
    identities = {}
    for key in ("actor", "triggering_actor"):
        value = run.get(key)
        if not isinstance(value, dict):
            raise Refusal("lane confirmation actual observer actor is absent")
        identities[key] = {field: value.get(field) for field in ("id", "login")}
    if original.actors(identities) != pins["observer"]["actors"]:
        raise Refusal("lane confirmation actual observer actors differ from independent pins")
    repository = pins["dispatch"]["target"]["repository"]
    actors = admission.actors(api, run, repository)
    identities = {key: {field: actors[key][field] for field in ("id", "login")} for key in actors}
    if identities != pins["observer"]["actors"]:
        raise Refusal("lane confirmation actual observer actors differ from independent pins")
    product = admission.repository(api.repository(repository), repository)
    return {"workflow": workflow, "actors": actors, "repository": product}


def observe(pins, retained, trusted):
    context, control, product = trusted
    api = Routing(pins["dispatch"]["target"]["repository"], control, product)
    current = observer(pins, context, api)
    source = history.qualify(control, entry.policy()["repository"], pins["context"])
    request = retained["intent"]["request"]
    custody = reservation.custody(request, pins["context"])
    before = original.observe(pins["dispatch"], {"request": request, "custody": custody, "actors": pins["actors"]}, control)
    return dict(current, original=before, source=source)
