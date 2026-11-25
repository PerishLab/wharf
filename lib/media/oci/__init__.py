from lib.media.oci import build, registry, spec
from lib.media.oci.build import context, smoked, source_context, web_context, web_smoked
from lib.media.oci.registry import Probe, channeled, exists, labelled, public_digest, registry_digest, released
from lib.media.oci.spec import (
    CHANNEL,
    CONTAINERFILE,
    HOST,
    LABEL,
    PLATFORM,
    Component,
    Image,
    SourceImage,
    WebImage,
    carried,
    components,
    reference,
    tagged,
)
from lib.process import run


def probe():
    return Probe(exists=exists, public_digest=public_digest, registry_digest=registry_digest, labelled=labelled)


def publish(request, runner=run, confirm=None):
    return build.publish(request, runner, confirm, probe())


def publish_source(request, runner=run, confirm=None):
    return build.publish_source(request, runner, confirm, probe())


def publish_web(request, runner=run, confirm=None):
    return build.publish_web(request, runner, confirm, probe())


def current(image, runner=run):
    return registry.current(image, runner, probe())


def advance(image, runner=run, confirm=None):
    return registry.advance(image, runner, confirm, probe())


__all__ = [
    "CHANNEL",
    "CONTAINERFILE",
    "HOST",
    "LABEL",
    "PLATFORM",
    "Component",
    "Image",
    "Probe",
    "SourceImage",
    "WebImage",
    "advance",
    "build",
    "carried",
    "channeled",
    "components",
    "context",
    "current",
    "exists",
    "labelled",
    "probe",
    "public_digest",
    "reference",
    "registry",
    "registry_digest",
    "released",
    "smoked",
    "source_context",
    "spec",
    "tagged",
    "web_context",
    "web_smoked",
    "publish",
    "publish_source",
    "publish_web",
]
