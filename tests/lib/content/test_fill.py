import unittest

from lib.content import fill
from lib.refusal import Refusal


class Fill(unittest.TestCase):
    def test_a_named_value_is_put_where_it_is_asked_for(self):
        self.assertEqual(fill.fill("hello {who}, {who}", {"who": "world"}), "hello world, world")

    def test_doubled_braces_are_the_braces_themselves(self):
        self.assertEqual(fill.fill("${{HOME}}/{product}", {"product": "concord"}), "${HOME}/concord")

    def test_a_name_nothing_carries_refuses(self):
        with self.assertRaisesRegex(Refusal, "{missing}"):
            fill.fill("a {missing} b", {"product": "concord"})

    def test_an_unclosed_brace_refuses(self):
        with self.assertRaisesRegex(Refusal, "unclosed"):
            fill.fill("a {product", {"product": "concord"})

    def test_an_empty_name_refuses(self):
        with self.assertRaisesRegex(Refusal, "empty"):
            fill.fill("a {} b", {})

    def test_a_brace_closing_nothing_refuses(self):
        with self.assertRaisesRegex(Refusal, "outside a variable"):
            fill.fill("a } b", {})

    def test_a_template_carrying_no_names_comes_back_as_it_was(self):
        self.assertEqual(fill.fill("plain text\nwith lines\n", {}), "plain text\nwith lines\n")
