from lib.content import resources

SPEC = resources.read_json("identity/format.json")
MAGIC = SPEC["magic"].encode()
SIZE = SPEC["size"]
SECTIONS = SPEC["sections"]
LAYOUT = {name: (field["offset"], field["offset"] + field["length"]) for name, field in SPEC["layout"].items()}
PAYLOAD = LAYOUT["payload"][0]
FIELDS = tuple(SPEC["binding"]["fields"])
MARKER = SPEC["binding"]["marker"]
DIGITS = {field: SPEC["binding"][field] for field in ("digest", "commit", "workload")}
