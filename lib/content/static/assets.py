import hashlib
import os
import re
import stat
import xml.etree.ElementTree as ElementTree
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from lib.content import canonical
from lib.refusal import Refusal

IDENTITY = "__wharf_preview.json"
CSP = "default-src 'none'; script-src 'none'; object-src 'none'; connect-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self'; font-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
HEADERS = f"/*\n  Content-Security-Policy: {CSP}\n  X-Content-Type-Options: nosniff\n  X-Robots-Tag: noindex, nofollow\n  Cache-Control: no-store\n".encode()
EXTENSIONS = {".html", ".css", ".svg", ".png", ".jpg", ".jpeg", ".ico", ".woff", ".woff2"}
TAGS = set("html head meta title body header nav main section article aside footer h1 h2 h3 h4 h5 h6 p span div a img style link button ul ol li figure figcaption br hr strong em small code pre table thead tbody tr th td details summary".split())
ATTRIBUTES = set("id class title lang dir charset name content href src alt width height rel type role tabindex disabled open colspan rowspan aria-label aria-hidden aria-labelledby aria-describedby".split())
SVG = set("svg g path rect circle ellipse line polyline polygon defs linearGradient radialGradient stop clipPath mask title desc use".split())
SVG_ATTRIBUTES = set("id class style viewBox width height version role aria-label aria-hidden aria-labelledby aria-describedby fill fill-rule fill-opacity stroke stroke-width stroke-linecap stroke-linejoin stroke-dasharray stroke-dashoffset stroke-opacity opacity d x y x1 y1 x2 y2 cx cy r rx ry points transform href offset stop-color stop-opacity gradientUnits gradientTransform spreadMethod clip-path clipPathUnits mask maskUnits maskContentUnits preserveAspectRatio".split())
MAX_FILE = 10 * 1024 * 1024
MAX_TOTAL = 50 * 1024 * 1024


def path(value):
    if not isinstance(value, str):
        raise Refusal("static output path must be text")
    held = PurePosixPath(value)
    if not value or held.is_absolute() or str(held) != value or "\\" in value or any(not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", part) for part in held.parts):
        raise Refusal("static output path is not a normalized public path")
    if any(part.lower().startswith(("private", "secret", "credentials")) for part in held.parts):
        raise Refusal("static output includes a private path")
    return held


def reference(value, name):
    if not isinstance(value, str) or "\\" in value or any(ord(char) < 32 for char in value):
        raise Refusal("static reference is malformed")
    try:
        url = urlsplit(value)
    except ValueError as error:
        raise Refusal("static reference is malformed") from error
    if url.scheme or url.netloc or value.startswith("//"):
        raise Refusal("static references cannot fetch or navigate to external content")
    if not url.path:
        return None
    parts = [] if url.path.startswith("/") else list(PurePosixPath(name).parent.parts)
    for part in unquote(url.path).split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise Refusal("static reference escapes the content root")
            parts.pop()
        else:
            parts.append(part)
    return str(path("/".join(parts)))


def css(text, name):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    if "\\" in text or re.search(r"@import|expression\s*\(|behavior\s*:|-moz-binding", text, re.I):
        raise Refusal("static CSS contains an active or external-content construct")
    return [reference(value.strip().strip("\"'"), name) for value in re.findall(r"url\s*\(([^)]*)\)", text, re.I)]


class Html(HTMLParser):
    def __init__(self, name):
        super().__init__(convert_charrefs=True)
        self.name = name
        self.references = []
        self.styling = False

    def handle_starttag(self, tag, attrs):
        if tag not in TAGS or len(dict(attrs)) != len(attrs):
            raise Refusal("static HTML contains unsupported tags or repeated attributes")
        for key, value in attrs:
            if key == "style" and value is not None:
                self.references.extend(css(value, self.name))
            elif key not in ATTRIBUTES:
                raise Refusal("static HTML contains an active or unsupported attribute")
            if key in ("href", "src"):
                self.references.append(reference(value, self.name))
        if tag == "link" and dict(attrs).get("rel") not in ("stylesheet", "icon"):
            raise Refusal("static HTML link must be a stylesheet or icon")
        self.styling = tag == "style" or self.styling

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "style":
            self.styling = False

    def handle_data(self, data):
        if self.styling:
            self.references.extend(css(data, self.name))


def svg(body, name):
    if re.search(br"<!DOCTYPE|<!ENTITY", body, re.I):
        raise Refusal("static SVG must not contain XML declarations with entities")
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as error:
        raise Refusal("static SVG is malformed") from error
    references = []
    if root.tag.removeprefix("{http://www.w3.org/2000/svg}") != "svg":
        raise Refusal("static SVG needs an svg root element")
    for node in root.iter():
        if node.tag.removeprefix("{http://www.w3.org/2000/svg}") not in SVG:
            raise Refusal("static SVG contains active or unsupported elements")
        for key, value in node.attrib.items():
            local = key.rsplit("}", 1)[-1]
            if local not in SVG_ATTRIBUTES:
                raise Refusal("static SVG contains active or unsupported attributes")
            if local == "href":
                if not value.startswith("#"):
                    raise Refusal("static SVG href must reference its own document")
            references.extend(css(value, name))
    return references


def validate(name, body):
    suffix = path(name).suffix.lower()
    if name in (IDENTITY, "_headers", "_redirects") or suffix not in EXTENSIONS or not body or len(body) > MAX_FILE:
        raise Refusal("static output contains a reserved, unsupported, empty or oversized file")
    if re.search(br"-----BEGIN [A-Z ]*PRIVATE KEY-----", body):
        raise Refusal("static output contains private key material")
    if suffix == ".svg":
        return svg(body, name)
    if suffix in (".html", ".css"):
        try:
            text = body.decode("utf-8")
        except UnicodeError as error:
            raise Refusal("static text must be UTF-8") from error
        if suffix == ".css":
            return css(text, name)
        parser = Html(name)
        parser.feed(text)
        parser.close()
        return parser.references
    signatures = {".png": b"\x89PNG\r\n\x1a\n", ".jpg": b"\xff\xd8\xff", ".jpeg": b"\xff\xd8\xff", ".ico": b"\x00\x00\x01\x00", ".woff": b"wOFF", ".woff2": b"wOF2"}
    if not body.startswith(signatures[suffix]):
        raise Refusal("static binary content disagrees with its file type")
    return []


def vetted(files):
    if not isinstance(files, dict) or not files or len(files) > 1000:
        raise Refusal("static content exceeds its file/byte budget or is empty")
    if any(not isinstance(body, bytes) for body in files.values()) or sum(map(len, files.values())) > MAX_TOTAL:
        raise Refusal("static content must carry bounded byte bodies")
    if "index.html" not in files:
        raise Refusal("static content needs an index.html entry")
    for name, body in files.items():
        if not isinstance(body, bytes):
            raise Refusal("static content must carry byte bodies")
        for linked in validate(name, body):
            if linked is not None and linked not in files:
                raise Refusal(f"static reference {linked} names absent content")
    return files


def collect(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise Refusal("static output must be a real directory")
    files = {}
    for entry in sorted(root.rglob("*")):
        mode = entry.lstat()
        if not stat.S_ISDIR(mode.st_mode) and (not stat.S_ISREG(mode.st_mode) or mode.st_nlink != 1):
            raise Refusal("static output cannot contain symlinks, hardlinks or special files")
        name = str(entry.relative_to(root))
        path(name)
        if stat.S_ISREG(mode.st_mode):
            if mode.st_size > MAX_FILE or len(files) >= 1000:
                raise Refusal("static output exceeds its file budget")
            files[name] = read(entry)
            if sum(map(len, files.values())) > MAX_TOTAL:
                raise Refusal("static output exceeds its total byte budget")
    return vetted(files)


def read(entry):
    try:
        descriptor = os.open(entry, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_FILE:
                raise Refusal("static output changed to an unsafe file")
            body = stream.read(MAX_FILE + 1)
            after = os.fstat(stream.fileno())
            if len(body) > MAX_FILE or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise Refusal("static output changed while reading")
            return body
    except OSError as error:
        raise Refusal("static output cannot be read without following links") from error


def manifest(files, intent):
    vetted(files)
    source = dict(intent["source"], repository=intent["repository"])
    entries = [{"path": name, "size": len(body), "sha256": hashlib.sha256(body).hexdigest()} for name, body in sorted(files.items())]
    document = {"schema": "wharf.preview.content/v1", "source": source, "files": entries, "policy": hashlib.sha256(HEADERS).hexdigest()}
    return dict(document, digest=canonical.digest(document))
