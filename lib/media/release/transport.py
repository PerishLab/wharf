import http.client
import time
import urllib.error
import urllib.request

ATTEMPTS = 3
BACKOFF = 0.25


def retryable(error):
    if isinstance(error, urllib.error.HTTPError):
        return 500 <= error.code < 600
    return isinstance(error, (urllib.error.URLError, http.client.IncompleteRead, TimeoutError, OSError))


def read(url):
    request = urllib.request.Request(url, headers={"User-Agent": "wharf"})
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
        declared = response.headers.get("Content-Length") if response.headers else None
        if declared is not None and declared.isdigit() and len(body) < int(declared):
            raise http.client.IncompleteRead(body, int(declared) - len(body))
        return body


def fetch(url):
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return read(url)
        except Exception as error:
            if not retryable(error) or attempt == ATTEMPTS:
                raise
            time.sleep(BACKOFF * attempt)
