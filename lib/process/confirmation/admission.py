import os

from lib.content.static.github import GitHub
from lib.process.confirmation import authority, policy, store
from lib.process.lane import entry
from lib.refusal import Refusal
from lib.store import r2
from lib.store.lane import documents, public

WRITERS = ("CLOUDFLARE_API_TOKEN", "WHARF_LANE_PROVIDER_TOKEN", "WHARF_CLOUDFLARE_TOKEN",
           "WHARF_R2_ACCESS_KEY_ID", "WHARF_R2_SECRET_ACCESS_KEY")
PREFIXES = ("WHARF_LANE_STATE_", "WHARF_LANE_EVIDENCE_", "WHARF_WORKLOAD_", "WHARF_RELEASES_", "WHARF_YARD_")


def admit(intent, pins, readers, trusted):
    intent, pins = policy.checked(intent, pins)
    if policy.context(trusted[0]) != pins["observer"]["context"]:
        raise Refusal("lane confirmation platform context differs before dependent reads")
    bucket, reader = readers
    prepared = public.prepare(pins["publication"])
    if not isinstance(reader, public.Reader) or reader != prepared:
        raise Refusal("lane confirmation reader differs from independently prepared public origin")
    before = store.read(pins, bucket, reader)
    observed = authority.observe(pins, before, trusted)
    if store.read(pins, bucket, reader) != before:
        raise Refusal("lane confirmation protected or public facts moved during authority observation")
    if authority.observe(pins, before, trusted) != observed:
        raise Refusal("lane confirmation observer or original authority facts moved during revalidation")
    if store.read(pins, bucket, reader) != before:
        raise Refusal("lane confirmation protected or public facts moved after authority revalidation")
    return documents.decode(documents.encode({"schema": "wharf.lane.confirmation-admission/v1", "selector": intent,
                            "pins": policy.bound(pins), "retained": before, "authority": observed}))


def observe(body, pins, environ=os.environ):
    intent, pins = policy.checked(policy.requested(body), pins)
    if any(value and (key in WRITERS or key.startswith(PREFIXES)) for key, value in environ.items()):
        raise Refusal("lane confirmation admission may hold prepared readers only")
    context = entry.platform(environ)
    if context != pins["observer"]["context"]:
        raise Refusal("lane confirmation current platform differs from its independent prepared observer")
    reader = store.Reader(pins["dispatch"], environ)
    control = GitHub(r2.secret(environ, "WHARF_PREVIEW_CONTROL_TOKEN"))
    product = GitHub(r2.secret(environ, "WHARF_PREVIEW_PRODUCT_TOKEN"))
    return admit(intent, pins, (reader, public.prepare(pins["publication"])), (context, control, product))
