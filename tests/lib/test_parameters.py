import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib import parameters
from lib.refusal import Refusal

NAMES = ("repository", "marker")


def resolved(names, argv, environ):
    return parameters.resolve("record", names, argv, environ)


def written(body):
    path = Path(tempfile.mkdtemp()) / "config.toml"
    path.write_text(body)
    return str(path)


class Priority(unittest.TestCase):
    def test_a_flag_outranks_the_environment(self):
        values, origins = resolved(NAMES, ["--marker", "v1", "--repository", "o/r"], {"WHARF_MARKER": "v2"})
        self.assertEqual(values["marker"], "v1")
        self.assertEqual(origins["marker"], "flag")

    def test_the_environment_outranks_the_configuration(self):
        path = written('marker = "v3"\nrepository = "o/r"\n')
        values, origins = resolved(NAMES, ["-c", path], {"WHARF_MARKER": "v2"})
        self.assertEqual((values["marker"], origins["marker"]), ("v2", "environment"))
        self.assertEqual((values["repository"], origins["repository"]), ("o/r", "configuration"))

    def test_the_configuration_outranks_the_default(self):
        path = written('source = "../elsewhere"\n')
        with mock.patch.dict(parameters.DEFAULTS, {"source": "../product"}):
            values, origins = resolved(("source",), ["-c", path], {})
        self.assertEqual((values["source"], origins["source"]), ("../elsewhere", "configuration"))

    def test_a_default_is_the_last_word(self):
        with mock.patch.dict(parameters.DEFAULTS, {"source": "../product"}):
            values, origins = resolved(("source",), [], {})
        self.assertEqual((values["source"], origins["source"]), ("../product", "default"))

    def test_an_action_section_overlays_the_top_level_key_by_key(self):
        path = written('marker = "v1"\nrepository = "o/r"\n\n[record]\nmarker = "v9"\n')
        values, origins = resolved(NAMES, ["-c", path], {})
        self.assertEqual(values, {"marker": "v9", "repository": "o/r"})
        self.assertEqual(origins["repository"], "configuration")


class Absence(unittest.TestCase):
    def test_a_parameter_set_nowhere_names_every_source_it_was_looked_for_in(self):
        with self.assertRaises(Refusal) as refused:
            resolved(NAMES, [], {})
        self.assertIn("marker: not --marker, not WHARF_MARKER, not in no configuration was given, and no default", str(refused.exception))

    def test_every_missing_parameter_is_listed_at_once(self):
        with self.assertRaises(Refusal) as refused:
            resolved(NAMES, [], {})
        self.assertIn("marker:", str(refused.exception))
        self.assertIn("repository:", str(refused.exception))

    def test_the_configuration_that_was_read_is_named(self):
        path = written("")
        with self.assertRaises(Refusal) as refused:
            resolved(("marker",), ["-c", path], {})
        self.assertIn(f"not in configuration {path}", str(refused.exception))

    def test_an_empty_environment_value_is_refused_rather_than_passed_on(self):
        with self.assertRaises(Refusal) as refused:
            resolved(("marker",), [], {"WHARF_MARKER": ""})
        self.assertIn("marker is set to an empty value", str(refused.exception))

    def test_an_empty_flag_value_is_refused_rather_than_eating_the_next_token(self):
        with self.assertRaises(Refusal) as refused:
            resolved(NAMES, ["--marker=", "--repository", "o/r"], {})
        self.assertIn("marker is set to an empty value", str(refused.exception))


class Credentials(unittest.TestCase):
    def test_a_declared_parameter_shaped_like_a_credential_is_refused(self):
        with mock.patch.dict(parameters.TYPES, {"npm-token": "string"}):
            with self.assertRaises(Refusal) as refused:
                resolved(("npm-token",), ["--npm-token", "x"], {})
        self.assertIn("credentials never pass through parameters: npm-token", str(refused.exception))

    def test_a_credential_written_into_a_configuration_file_is_refused(self):
        path = written('marker = "v1"\nr2_access_key_id = "AKIA"\n')
        with self.assertRaises(Refusal) as refused:
            resolved(("marker",), ["-c", path], {})
        self.assertIn("r2_access_key_id", str(refused.exception))

    def test_a_key_that_is_a_workload_key_is_not_mistaken_for_a_credential(self):
        with mock.patch.dict(parameters.TYPES, {"binary-key": "string"}):
            values, _ = resolved(("binary-key",), ["--binary-key", "abc"], {})
        self.assertEqual(values["binary-key"], "abc")


class Types(unittest.TestCase):
    def test_an_environment_string_becomes_the_declared_type(self):
        with mock.patch.dict(parameters.TYPES, {"full": "bool", "attempt": "int", "put": "strings"}):
            values, _ = resolved(("full", "attempt", "put"), [], {"WHARF_FULL": "true", "WHARF_ATTEMPT": "2", "WHARF_PUT": "a\nb\n"})
        self.assertEqual(values, {"full": True, "attempt": 2, "put": ["a", "b"]})

    def test_a_value_that_is_not_the_declared_type_refuses(self):
        with mock.patch.dict(parameters.TYPES, {"full": "bool"}):
            with self.assertRaises(Refusal) as refused:
                resolved(("full",), [], {"WHARF_FULL": "yes"})
        self.assertIn("neither true nor false", str(refused.exception))

    def test_a_configuration_value_of_the_wrong_type_refuses(self):
        path = written("marker = 1\n")
        with self.assertRaises(Refusal) as refused:
            resolved(("marker",), ["-c", path], {})
        self.assertIn("marker must be string", str(refused.exception))

    def test_a_bare_flag_sets_a_bool(self):
        with mock.patch.dict(parameters.TYPES, {"full": "bool"}):
            values, _ = resolved(("full",), ["--full"], {})
        self.assertEqual(values["full"], True)

    def test_a_list_takes_every_occurrence_of_its_flag(self):
        with mock.patch.dict(parameters.TYPES, {"put": "strings"}):
            values, _ = resolved(("put",), ["--put", "a", "--put", "b"], {})
        self.assertEqual(values["put"], ["a", "b"])

    def test_a_configuration_array_replaces_rather_than_appends(self):
        path = written('put = ["a", "b"]\n\n[record]\nput = ["c"]\n')
        with mock.patch.dict(parameters.TYPES, {"put": "strings"}):
            values, _ = resolved(("put",), ["-c", path], {})
        self.assertEqual(values["put"], ["c"])


class Shape(unittest.TestCase):
    def test_an_undeclared_parameter_refuses(self):
        with self.assertRaises(Refusal) as refused:
            resolved(NAMES, ["--nonsense", "x"], {})
        self.assertIn("nonsense is not declared", str(refused.exception))

    def test_a_declared_parameter_this_action_does_not_take_refuses(self):
        with self.assertRaises(Refusal) as refused:
            resolved(NAMES, ["--marker", "v1", "--repository", "o/r", "--tree", "t"], {})
        self.assertIn("not taken by this action: tree", str(refused.exception))

    def test_a_scalar_given_twice_refuses(self):
        with self.assertRaises(Refusal) as refused:
            resolved(("marker",), ["--marker", "v1", "--marker", "v2"], {})
        self.assertIn("marker was given 2 times", str(refused.exception))

    def test_a_missing_configuration_file_refuses(self):
        with self.assertRaises(Refusal) as refused:
            resolved(("marker",), ["-c", "/nowhere/config.toml"], {})
        self.assertIn("does not exist", str(refused.exception))

    def test_a_configuration_file_that_is_not_toml_refuses(self):
        path = written("marker = \n")
        with self.assertRaises(Refusal) as refused:
            resolved(("marker",), ["-c", path], {})
        self.assertIn("is not TOML", str(refused.exception))

    def test_a_bare_word_is_not_a_parameter(self):
        with self.assertRaises(Refusal) as refused:
            resolved(("marker",), ["record"], {})
        self.assertIn("is not a --parameter", str(refused.exception))

    def test_a_flag_without_a_value_refuses(self):
        with self.assertRaises(Refusal) as refused:
            resolved(("marker",), ["--marker"], {})
        self.assertIn("--marker was given without a value", str(refused.exception))


class Action(unittest.TestCase):
    def test_the_first_word_is_the_action(self):
        self.assertEqual(parameters.acted("plan", {"record": None}, ["record", "--marker", "v1"]), ("record", ["--marker", "v1"]))

    def test_no_word_at_all_refuses(self):
        with self.assertRaisesRegex(Refusal, "plan takes one action: record"):
            parameters.acted("plan", {"record": None}, [])


class Report(unittest.TestCase):
    def test_a_long_value_is_abbreviated_rather_than_flooding_the_log(self):
        values, origins = resolved(("steps",), [], {"WHARF_STEPS": "x" * 500})
        self.assertIn("(502 characters)", parameters.report(values, origins)[0])
        self.assertLess(len(parameters.report(values, origins)[0]), 140)

    def test_every_resolved_parameter_is_shown_with_where_it_came_from(self):
        values, origins = resolved(NAMES, ["--marker", "v1"], {"WHARF_REPOSITORY": "o/r"})
        self.assertEqual(parameters.report(values, origins), ["marker: 'v1' (flag)", "repository: 'o/r' (environment)"])


if __name__ == "__main__":
    unittest.main()
