import ast
import unittest

from lib.check import parameters


class Taken(unittest.TestCase):
    def test_only_sequences_of_names_count_as_parameters(self):
        tree = ast.parse('ACTIONS = {"lodge": (lodge, ["repository", "marker"])}\nheld = target["runner"]\n')
        self.assertEqual(sorted(name for names in parameters.sequences(tree) for name in names), ["marker", "repository"])

    def test_a_starred_list_keeps_the_names_beside_it(self):
        tree = ast.parse('CONTEXT = ("repository", "marker")\nTAKEN = [*CONTEXT, "planned"]\n')
        self.assertEqual(sorted(name for names in parameters.sequences(tree) for name in names), ["marker", "planned", "repository"])


class Declared(unittest.TestCase):
    def test_every_declared_parameter_is_taken_by_an_action(self):
        self.assertEqual(parameters.check(*self.repository()), [])

    def repository(self):
        from lib.check import files
        from lib.process import git

        root = git(".", "rev-parse", "--show-toplevel")
        return root, files.listed(root)
