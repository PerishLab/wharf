import copy
import json
import unittest
from unittest import mock

from lib.cargo import toolchain
from lib.media import node
from lib.refusal import Refusal
from lib.store import handoff
from tests.lib.content.test_assets import FILES
from tests.lib.content.test_preview import target
from tests.lib.content.test_static import StaticRepository
from tests.lib.media.test_preview import Builder, DOMAIN, VERSIONS


class PreviewDomain(unittest.TestCase):
    def test_repository_tool_pins_refuse_instead_of_selecting_versions(self):
        repository = StaticRepository()
        for name in ("package.json", "apps/review/package.json", "packages/lib/package.json"):
            original = (repository.root / name).read_text()
            for key in ("engines", "packageManager"):
                with self.subTest(name=name, key=key):
                    repository.write(name, json.dumps(dict(json.loads(original), **{key: None})))
                    repository.commit()
                    with self.assertRaisesRegex(Refusal, "must not declare tool versions"):
                        repository.request()
            repository.write(name, original)
            repository.commit()

    def test_missing_or_malformed_domain_refuses_before_product_execution(self):
        for value in (None, "", "not-json", "{}", '{"node.version":"latest"}'):
            builder = Builder()
            env = builder.environment()
            env.pop(toolchain.VARIABLE, None)
            if value is not None:
                env[toolchain.VARIABLE] = value
            request = node.Preview(builder.repository.root, builder.destination, builder.intent, target())
            with self.subTest(value=value), self.assertRaises(Refusal):
                node.preview_build(request, builder.execute, builder.inspect, env)
            self.assertEqual(builder.calls, [])
            self.assertFalse(builder.destination.exists())

    def test_domain_selects_versions_without_changing_source_declaration(self):
        builder = Builder()
        requested = copy.deepcopy(builder.intent)
        domain = dict(DOMAIN, **{"node.version": "25.0.0", "pnpm.version": "12.0.0"})
        versions = dict(VERSIONS, node="v25.0.0", pnpm="12.0.0")
        with mock.patch.dict(VERSIONS, versions):
            result = builder.build({toolchain.VARIABLE: toolchain.encoded(domain)})
        self.assertEqual(builder.intent, requested)
        tools = result["basis"]["receipt"]["tools"]
        self.assertEqual(tools["node"]["version"], "v25.0.0")
        self.assertEqual(tools["pnpm"]["version"], "12.0.0")
        receipt = copy.deepcopy(result["basis"]["receipt"])
        receipt["tools"]["node"]["version"] = VERSIONS["node"]
        changed, _ = handoff.describe(FILES, receipt)
        self.assertNotEqual(result["key"], changed["key"])

    def test_each_prepared_tool_must_match_the_domain(self):
        for name in ("node.version", "pnpm.version"):
            builder = Builder()
            domain = dict(DOMAIN, **{name: "1.0.0"})
            with self.subTest(name=name), self.assertRaisesRegex(Refusal, "trusted domain"):
                builder.build({toolchain.VARIABLE: toolchain.encoded(domain)})
            self.assertFalse(any(kind == "execute" for kind, _, _ in builder.calls))

    def test_domain_drift_refuses_handoff(self):
        builder = Builder()
        env = builder.environment()
        original = builder.execute

        def execute(argv, cwd, child):
            original(argv, cwd, child)
            env[toolchain.VARIABLE] = toolchain.encoded(dict(DOMAIN, **{"node.version": "1.0.0"}))

        request = node.Preview(builder.repository.root, builder.destination, builder.intent, target())
        with self.assertRaisesRegex(Refusal, "domain versions changed"):
            node.preview_build(request, execute, builder.inspect, env)
        self.assertFalse(builder.destination.exists())


if __name__ == "__main__":
    unittest.main()
