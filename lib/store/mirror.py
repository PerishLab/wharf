import threading
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from lib.refusal import Refusal
from lib.store.directory import Directory

LOOPBACK = "127.0.0.1"


def translated(body, authority, local):
    return body.replace(authority.encode(), local.encode())


class Mirrored(SimpleHTTPRequestHandler):
    def do_GET(self):
        key = self.path.split("?", 1)[0].lstrip("/")
        try:
            body = Directory(self.directory).get(key)
        except Refusal:
            self.send_error(404)
            return
        if key.endswith(".json"):
            body = translated(body, self.server.authority, self.server.url)
        self.send_response(200)
        self.send_header("Content-Type", self.guess_type(key))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


@contextmanager
def served(root, authority):
    server = ThreadingHTTPServer((LOOPBACK, 0), partial(Mirrored, directory=str(root)))
    server.authority = authority
    server.url = f"http://{LOOPBACK}:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.url
    finally:
        server.shutdown()
        server.server_close()
