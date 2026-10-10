import http.client
import re
from dataclasses import dataclass

from lib.content.lane import codec, identity
from lib.refusal import Refusal
from lib.store.lane import documents

LIMIT = 65536
HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")


def location(reference):
    reference = codec.read("reference", reference)
    return f"lane/v1/evidence/{reference['digest']}.json"


def prepare(configuration):
    configuration = documents.object_value(configuration, ("authority", "origin"))
    origin = configuration["origin"]
    if not isinstance(origin, str) or not origin.startswith("https://"):
        raise Refusal("lane public authority requires a prepared bare HTTPS origin")
    return Reader(configuration["authority"], origin[8:])


@dataclass(frozen=True)
class Reader:
    authority: str
    host: str

    def __post_init__(self):
        identity.atom(self.authority)
        if not isinstance(self.host, str) or len(self.host) > 253 or not HOST.fullmatch(self.host) or self.host.endswith((".localhost", ".local")):
            raise Refusal("lane public authority requires an exact lowercase DNS origin without routing extras")

    def reference(self, digest):
        return codec.read("reference", {"authority": self.authority, "digest": digest})

    def read(self, reference):
        reference = codec.read("reference", reference)
        if reference["authority"] != self.authority:
            raise Refusal("lane public reference belongs to another prepared authority")
        connection = None
        try:
            connection = http.client.HTTPSConnection(self.host, timeout=30)
            connection.request("GET", "/" + location(reference), headers={"Accept": "application/json", "Accept-Encoding": "identity"})
            response = connection.getresponse()
            if response.status != 200:
                raise Refusal("lane immutable public readback is unavailable; no redirect or private fallback")
            if response.getheader("Content-Encoding") not in (None, "identity"):
                raise Refusal("lane public readback cannot transform encoded content")
            body = response.read(LIMIT + 1)
            if not isinstance(body, bytes) or len(body) > LIMIT:
                raise Refusal("lane public readback exceeds its bounded byte contract")
            return body
        except (OSError, http.client.HTTPException) as error:
            raise Refusal("lane public readback failed; no private fallback") from error
        finally:
            if connection is not None:
                try:
                    connection.close()
                except (OSError, http.client.HTTPException) as error:
                    raise Refusal("lane public readback connection cleanup failed") from error
