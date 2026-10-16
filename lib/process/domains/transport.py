import urllib.request

from lib.refusal import Refusal


class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *arguments):
        raise Refusal("static provider redirects are not admitted")
