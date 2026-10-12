from lib.content import resources

SUITE = resources.read_json("identity/coverage.json")


def tested(snapshot):
    held = snapshot.get("guard") if isinstance(snapshot, dict) else None
    if not isinstance(held, dict) or held.get("ok") is not True or held.get("strength") != "full":
        return False
    proof = held.get("guard")
    actions = proof.get("actions") if isinstance(proof, dict) else None
    return isinstance(actions, list) and any(isinstance(action, dict) and action.get("name") == SUITE["action"] for action in actions)
