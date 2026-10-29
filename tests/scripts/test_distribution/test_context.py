import copy
import json
import os
import unittest
from unittest import mock

from lib.process.trial import context
from lib.refusal import Refusal
from tests.scripts.test_distribution import publisher
from tests.scripts.test_distribution.world import OBSERVER, producer


class Native(unittest.TestCase):
    def test_repository_endpoint_has_no_trailing_slash(self):
        with mock.patch.object(context.subprocess, "check_output", return_value='{"id":1}') as request:
            self.assertEqual(context.api(""), {"id": 1})
        self.assertEqual(request.call_args.args[0], ["gh", "api", "repos/PerishLab/wharf"])

    def inventory(self):
        return {"total_count": 1, "jobs": [{"id": 30, "run_id": 20, "run_attempt": 1, "head_sha": "a" * 40, "head_branch": "main", "name": context.NAMES["windtun"], "status": "in_progress", "conclusion": None}]}

    def test_active_main_native_attempt_is_required(self):
        repository = {"id": 1, "full_name": "PerishLab/wharf", "default_branch": "main"}
        run = dict(publisher(), id=20, repository=repository, head_repository=repository, status="in_progress", conclusion=None)

        def api(path):
            if path == "":
                return repository
            return self.inventory() if "/jobs?" in path else run

        with mock.patch.object(context, "api", side_effect=api), mock.patch.dict(os.environ, {"GITHUB_JOB": "windtun"}):
            self.assertEqual(context.observe(json.dumps(OBSERVER), "windtun")[0], OBSERVER)
            for change in ({"head_branch": "topic"}, {"event": "pull_request"}, {"path": "other.yml"}, {"run_attempt": 2}, {"status": "completed"}, {"head_repository": dict(repository, id=2)}):
                with self.subTest(change=change), mock.patch.object(context, "api", side_effect=lambda path: repository if path == "" else dict(run, **change)), self.assertRaises(Refusal):
                    context.observe(OBSERVER, "windtun")
        with mock.patch.dict(os.environ, {"GITHUB_JOB": "other"}), self.assertRaises(Refusal):
            context.observe(OBSERVER, "windtun")

    def test_incomplete_duplicate_or_foreign_jobs_refuse(self):
        for change in ({"total_count": 2}, {"jobs": self.inventory()["jobs"] * 2, "total_count": 2}, {"jobs": [dict(self.inventory()["jobs"][0], run_attempt=2)]}):
            with self.subTest(change=change), mock.patch.object(context, "api", return_value=dict(self.inventory(), **change)), self.assertRaises(Refusal):
                context.native_jobs(OBSERVER)

    def test_missing_or_skipped_producer_steps_never_count_as_pass(self):
        held = producer()
        context.succeeded(held, [held["steps"][0]["name"]])
        for change in ({"conclusion": "cancelled"}, {"steps": []}, {"steps": held["steps"] * 2}):
            with self.subTest(change=change), self.assertRaises(Refusal):
                context.succeeded(dict(held, **change), [held["steps"][0]["name"]])
        changed = copy.deepcopy(held)
        changed["steps"][0]["conclusion"] = "skipped"
        with self.assertRaises(Refusal):
            context.succeeded(changed, [held["steps"][0]["name"]])

    def test_attempt_fields_are_exact_and_path_safe(self):
        for change in ({"attempt": "../2"}, {"run": "0"}, {"wharf": "x" * 40}, {"extra": True}):
            with self.subTest(change=change), self.assertRaises(Refusal):
                context.decode(dict(OBSERVER, **change))
