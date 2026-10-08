import unittest
from unittest import mock

from lib.content.static.github import Routing
from lib.refusal import Refusal


class IdentityRouting(unittest.TestCase):
    def setUp(self):
        self.control = mock.Mock()
        self.product = mock.Mock()
        self.routing = Routing("PerishLab/crest", self.control, self.product)

    def test_control_reads_do_not_reach_product_identity(self):
        self.routing.repository("PerishLab/wharf")
        self.routing.run(123)
        self.control.repository.assert_called_once_with("PerishLab/wharf")
        self.control.run.assert_called_once_with(123)
        self.assertFalse(self.product.mock_calls)

    def test_product_reads_do_not_reach_control_identity(self):
        self.routing.repository("PerishLab/crest")
        self.routing.permission("PerishLab/crest", "alice")
        self.routing.commit("PerishLab/crest", "a" * 40)
        self.product.repository.assert_called_once_with("PerishLab/crest")
        self.product.permission.assert_called_once_with("PerishLab/crest", "alice")
        self.product.commit.assert_called_once_with("PerishLab/crest", "a" * 40)
        self.assertFalse(self.control.mock_calls)

    def test_foreign_repository_refuses_before_any_read(self):
        for invoke in (lambda: self.routing.repository("PerishLab/design"), lambda: self.routing.permission("PerishLab/design", "alice"), lambda: self.routing.commit("PerishLab/design", "a" * 40)):
            with self.assertRaises(Refusal):
                invoke()
        self.assertFalse(self.control.mock_calls)
        self.assertFalse(self.product.mock_calls)

    def test_matching_repository_does_not_collapse_actor_or_run_routing(self):
        routing = Routing("PerishLab/wharf", self.control, self.product)
        routing.run(123)
        routing.permission("PerishLab/wharf", "alice")
        self.control.run.assert_called_once_with(123)
        self.product.permission.assert_called_once_with("PerishLab/wharf", "alice")


if __name__ == "__main__":
    unittest.main()
