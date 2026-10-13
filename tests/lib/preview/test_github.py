import io
import unittest
import urllib.error
from unittest import mock

from lib.content.static import github
from lib.refusal import Refusal


class Response(io.BytesIO):
    status = 200

    def geturl(self):
        return "https://api.github.com/repos/PerishLab/wharf"


class GitHub(unittest.TestCase):
    def client(self, body=b'{}'):
        api = github.GitHub("private-test-token")
        api._client = mock.Mock()
        api._client.open.return_value = Response(body)
        return api

    def test_fixed_get_and_bounded_read_only_request(self):
        api = self.client(b'{"id":123}')
        self.assertEqual(api.repository("PerishLab/wharf"), {"id": 123})
        request = api._client.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, "https://api.github.com/repos/PerishLab/wharf")
        self.assertEqual(api._client.open.call_args.kwargs, {"timeout": 30})
        self.assertNotIn("private-test-token", repr(api))

    def test_unsafe_paths_fail_without_http(self):
        paths = ("https://attacker.invalid", "/repos/../wharf", "/repos/PerishLab/wharf?x=1", "/repos/PerishLab/wharf/actions/workflows/ship.yml/dispatches", "/repos/PerishLab/wharf/git/commits/main", "/repos/PerishLab/wharf/collaborators/a%2Fb/permission")
        api = self.client()
        for path in paths:
            with self.subTest(path=path), self.assertRaises(Refusal):
                api.get(path)
        api._client.open.assert_not_called()

    def test_redirects_and_changed_endpoint_refuse(self):
        with self.assertRaises(Refusal):
            github.Redirect().redirect_request(None, None, 302, "redirect", {}, "https://attacker.invalid")
        api = self.client()
        api._client.open.return_value.geturl = lambda: "https://attacker.invalid"
        with self.assertRaises(Refusal):
            api.repository("PerishLab/wharf")

    def test_http_network_timeout_and_status_fail_closed(self):
        for error in (urllib.error.HTTPError("https://api.github.com", 403, "denied", {}, None), urllib.error.URLError("unavailable"), TimeoutError(), OSError()):
            api = self.client()
            api._client.open.side_effect = error
            with self.subTest(error=type(error).__name__), self.assertRaises(Refusal) as raised:
                api.repository("PerishLab/wharf")
            self.assertNotIn("private-test-token", str(raised.exception))
            self.assertEqual(api._client.open.call_count, 1)
        api = self.client()
        api._client.open.return_value.status = 404
        with self.assertRaises(Refusal):
            api.repository("PerishLab/wharf")

    def test_malformed_oversized_duplicate_and_nonobject_json_refuse(self):
        for body in (b"not-json", b'{"id":1,"id":2}', b"x" * (github.LIMIT + 1), b"[]", b"null", b"\xff"):
            with self.subTest(body=str(body)[:30]), self.assertRaises(Refusal):
                self.client(body).repository("PerishLab/wharf")

    def test_selectors_and_token_validation(self):
        for token in (None, "", "bad\ntoken"):
            with self.assertRaises(Refusal):
                github.GitHub(token)
        api = self.client()
        for invoke in (lambda: api.run(True), lambda: api.permission("PerishLab/wharf", "../actor"), lambda: api.commit("PerishLab/wharf", "main")):
            with self.assertRaises(Refusal):
                invoke()
        api._client.open.assert_not_called()

    def test_fixed_permission_commit_and_run_endpoint_selection(self):
        api = self.client()
        api.get = mock.Mock(return_value={})
        api.permission("PerishLab/crest", "automation[bot]")
        api.get.assert_called_with("/repos/PerishLab/crest/collaborators/automation%5Bbot%5D/permission")
        api.commit("PerishLab/crest", "a" * 40)
        api.get.assert_called_with("/repos/PerishLab/crest/git/commits/" + "a" * 40)
        api.run(123)
        api.get.assert_called_with("/repos/PerishLab/wharf/actions/runs/123")

    def test_jobs_selects_exact_original_attempt_and_only_fixed_identity_endpoint(self):
        api = self.client()
        api.get = mock.Mock(return_value={"total_count": 0, "jobs": []})
        self.assertEqual(api.jobs(123, 2), {"total_count": 0, "jobs": []})
        api.get.assert_called_once_with("/repos/PerishLab/wharf/actions/runs/123/attempts/2/jobs")
        for run, attempt in ((True, 1), (123, False), (0, 1), (123, -1), ("123", 1)):
            with self.assertRaises(Refusal):
                api.jobs(run, attempt)

    def test_real_jobs_transport_remains_bounded_get_without_pagination_or_mutation(self):
        api = self.client(b'{"total_count":0,"jobs":[]}')
        url = "https://api.github.com/repos/PerishLab/wharf/actions/runs/123/attempts/1/jobs"
        api._client.open.return_value.geturl = lambda: url
        self.assertEqual(api.jobs(123, 1), {"total_count": 0, "jobs": []})
        request = api._client.open.call_args.args[0]
        self.assertEqual(request.full_url, url)
        self.assertEqual(request.get_method(), "GET")
        for suffix in ("?page=2", "/logs", "/rerun"):
            with self.assertRaises(Refusal):
                api.get("/repos/PerishLab/wharf/actions/runs/123/attempts/1/jobs" + suffix)


if __name__ == "__main__":
    unittest.main()
