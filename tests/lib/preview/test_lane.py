import copy
import tomllib
import unittest

from lib.content.static import declaration
from lib.refusal import Refusal


def declared(app="review", directory="apps/review", package="@demo/review", resource="review"):
    return f'''[lane]
repository = "PerishLab/crest"
[lane.app.{app}]
path = "{directory}"
package = "{package}"
[lane.app.{app}.mapping]
provider = "cfworker"
access = "public"
account = "{'a' * 32}"
resource = "{resource}"
[lane.app.{app}.binding.preview]
adaptor = "static"
capabilities = ["inspect", "deploy"]
[lane.app.{app}.binding.preview.authorities]
authorization = "ensign"
publication = "wharf"
'''


class LaneDeclaration(unittest.TestCase):
    def document(self):
        return tomllib.loads(declared())

    def test_default_keyed_and_identity_separation(self):
        value = self.document()
        app = value["lane"]["app"]["review"]
        app["binding"]["preview.a"] = copy.deepcopy(app["binding"]["preview"])
        value["lane"]["app"] = {"design": app}
        self.assertEqual(declaration.read(value), {"design": app})
        self.assertEqual(set(app["binding"]), {"preview", "preview.a"})
        self.assertEqual(app["mapping"]["resource"], "review")
        self.assertEqual(declaration.read({}), {})

    def test_binding_names_and_capabilities_refuse(self):
        for name in ("preview.a.b", "preview-a", "Preview", "preview.", "preview.1a", "v1.2.3-lane.a", "preview." + "a" * 49):
            value = self.document()
            app = value["lane"]["app"]["review"]
            app["binding"] = {name: app["binding"]["preview"]}
            with self.subTest(name=name), self.assertRaises(Refusal):
                declaration.read(value)
        for actions in ([], ["deploy"], ["inspect", "deploy", "deploy"], ["inspect", "deploy", "publish"], ["inspect", "deploy", 1], "deploy"):
            value = self.document()
            value["lane"]["app"]["review"]["binding"]["preview"]["capabilities"] = actions
            with self.subTest(actions=actions), self.assertRaises(Refusal):
                declaration.read(value)

    def test_exact_fields_and_empty_tables_refuse(self):
        for path in ((), ("lane",), ("lane", "app", "review"), ("lane", "app", "review", "mapping"), ("lane", "app", "review", "binding", "preview"), ("lane", "app", "review", "binding", "preview", "authorities")):
            value = self.document()
            held = value
            for key in path:
                held = held[key]
            if not path:
                held["preview"] = {}
            else:
                held["token"] = "private"
            with self.subTest(path=path), self.assertRaises(Refusal):
                declaration.read(value)
        for path in (("app",), ("app", "review", "binding")):
            value = self.document()
            held = value["lane"]
            for key in path[:-1]:
                held = held[key]
            held[path[-1]] = {}
            with self.subTest(path=path), self.assertRaises(Refusal):
                declaration.read(value)

    def test_repository_mapping_authority_and_selector_bounds(self):
        cases = [
            (("repository",), "../crest"), (("repository",), "a" * 101 + "/crest"),
            (("app", "review", "mapping", "account"), "A" * 32),
            (("app", "review", "mapping", "resource"), "a" * 64),
            (("app", "review", "mapping", "resource"), "review.a"),
            (("app", "review", "binding", "preview", "adaptor"), "static.a"),
            (("app", "review", "binding", "preview", "authorities", "publication"), "wharf.a"),
            (("app", "review", "package"), "@demo/*"),
        ]
        for path, changed in cases:
            value = self.document()
            held = value["lane"]
            for key in path[:-1]:
                held = held[key]
            held[path[-1]] = changed
            with self.subTest(path=path), self.assertRaises(Refusal):
                declaration.read(value)
        value = self.document()
        app = value["lane"]["app"]["review"]
        app["mapping"]["resource"] = "1--" + "a" * 60
        app["binding"]["preview"]["capabilities"] += ["build", "dispose"]
        self.assertEqual(declaration.read(value), {"review": app})

    def test_quoted_keyed_toml_is_required(self):
        body = declared().replace("binding.preview", "binding.'preview.a'")
        self.assertEqual(set(declaration.read(tomllib.loads(body))["review"]["binding"]), {"preview.a"})
        with self.assertRaises(Refusal):
            declaration.read(tomllib.loads(body.replace("'preview.a'", "preview.a")))
