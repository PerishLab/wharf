import unittest

from lib.check.locking import literal
from lib.refusal import Refusal


class LiteralTests(unittest.TestCase):
    def test_admitted_maps_lists_and_scalars(self):
        body = b"version: '9.0'\nvalue: {a: true, b: [linux, 'arm64'], c: 2.5.3}\nnested:\n  list:\n    - esbuild\n    - '@types/node'\nempty: {}\n"
        self.assertEqual(literal.parse(body), {"version": "9.0", "value": {"a": True, "b": ["linux", "arm64"], "c": "2.5.3"}, "nested": {"list": ["esbuild", "@types/node"]}, "empty": {}})

    def test_duplicate_and_ambiguous_yaml_refuses(self):
        bodies = [b"a: 1\na: 2", b"a: {b: 1, b: 2}", b"a: &x {}\nb: *x", b"a: {<<: x}", b"a: !!str x", b"---\na: 1", b"a: 1\n---\nb: 2", b"%YAML 1.2\na: 1", b"a: |\n  secret", b"a: .inf", b"a: 2026-10-08", b"a: null", b"a: 9.0", b"a: 01", b"a: secret # comment", b"a: 'x''y'", b'a: "\\n"', b"a:\n    b: 1", b"a:\n  - b\n  c: 1", b"a: [b,]", b"a: 1\r\n", b"a:\tsecret", b"\xef\xbb\xbfa: 1", b"a:\n"]
        for body in bodies:
            with self.subTest(body=body), self.assertRaises(Refusal):
                literal.parse(body)

    def test_bounds_and_sanitized_refusal(self):
        for body in [b"x" * (literal.LIMIT + 1), b"a: " + b"x" * 4097, b"a: [" * 14 + b"a" + b"]" * 14, b"a: SECRET\na: SECRET"]:
            with self.subTest(size=len(body)), self.assertRaises(Refusal) as caught:
                literal.parse(body)
            self.assertNotIn("SECRET", str(caught.exception))

    def test_numeric_subset_preserves_installed_parser_precision(self):
        self.assertEqual(literal.parse(b"value: 9007199254740991"), {"value": 9007199254740991})
        for token in [b"9007199254740992", b"+0xFF", b"+0o17", b"1_000", b"1e3", b".NaN", b"\x7f"]:
            with self.subTest(token=token), self.assertRaises(Refusal):
                literal.parse(b"value: " + token)

    def test_node_budget(self):
        body = "".join(f"key{index}: value\n" for index in range(literal.NODES)).encode()
        with self.assertRaises(Refusal):
            literal.parse(body)
