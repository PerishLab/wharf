import unittest

from lib.check import commands

KNOWN = {"ship": {"binary", "release"}, "plan": {"record"}}


def refused(text):
    held = [commands.refused(command, KNOWN) for _, command in commands.blocks(text) if command not in commands.PREPARED]
    return [finding for finding in held if finding]


def workflow(run):
    return "jobs:\n  one:\n    steps:\n      - name: a step\n        run: " + run + "\n"


class Shape(unittest.TestCase):
    def test_a_bare_command_passes(self):
        self.assertEqual(refused(workflow("python3 -B -m scripts.ship binary")), [])

    def test_a_command_with_a_flag_is_refused(self):
        self.assertIn("is one python3", refused(workflow("python3 -B -m scripts.ship binary --source ../product"))[0])

    def test_shell_around_the_command_is_refused(self):
        text = workflow("|\n          key=$(python3 -B -m scripts.ship binary)\n          echo \"$key\"")
        self.assertIn("is one python3", refused(text)[0])

    def test_a_python_that_is_not_python3_is_refused(self):
        self.assertIn("is one python3", refused(workflow("python -B -m scripts.ship binary"))[0])

    def test_a_script_that_does_not_exist_is_refused(self):
        self.assertIn("scripts.invented, which does not exist", refused(workflow("python3 -B -m scripts.invented binary"))[0])

    def test_an_action_the_script_does_not_take_is_refused(self):
        self.assertIn("names the action smoke", refused(workflow("python3 -B -m scripts.plan smoke"))[0])

    def test_a_prepared_command_passes_only_as_written(self):
        self.assertEqual(refused(workflow("git config --global core.autocrlf false")), [])
        self.assertIn("is one python3", refused(workflow("git config --global core.autocrlf true"))[0])

    def test_a_block_scalar_prepared_command_passes(self):
        login = sorted(commands.PREPARED, key=len)[-1]
        text = workflow("|\n          " + login.replace("\n", "\n          "))
        self.assertEqual(refused(text), [])


class Entries(unittest.TestCase):
    def test_the_actions_a_script_takes_are_read_from_its_source(self):
        import tempfile
        from pathlib import PurePosixPath, Path

        root = Path(tempfile.mkdtemp())
        (root / "scripts").mkdir()
        (root / "scripts" / "demo.py").write_text('ACTIONS = {"one": None, "two": None}\n')
        self.assertEqual(commands.entries(root, [PurePosixPath("scripts/demo.py")]), {"demo": {"one", "two"}})


if __name__ == "__main__":
    unittest.main()


class Idle(unittest.TestCase):
    def test_an_action_a_workflow_runs_is_live(self):
        self.assertEqual(commands.idle({"plan": {"record"}}, {("plan", "record")}), [])

    def test_an_action_nothing_runs_is_named(self):
        held = commands.idle({"depot": {"lodge", "diff"}}, {("depot", "lodge")})
        self.assertEqual(held, ["scripts/depot.py takes the action diff, which no workflow runs"])

    def test_a_layer_unit_ship_runs_through_step_is_live(self):
        action = sorted(commands.LAYERED)[0]
        self.assertEqual(commands.idle({"ship": {action}}, {("ship", "step")}), [])
        self.assertEqual(len(commands.idle({"plan": {action}}, set())), 1)
