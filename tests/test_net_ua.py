"""USER_AGENT 必须跟随当前版本号（防止再次停留在 0.9.66）。"""
import unittest

from smm import net
from smm.api import APP_VERSION


class UserAgentTest(unittest.TestCase):
    def test_ua_carries_current_version(self):
        self.assertIn(APP_VERSION, net.USER_AGENT)

    def test_ua_has_product_name(self):
        self.assertTrue(net.USER_AGENT.startswith("HuifengsModManager/"))


if __name__ == "__main__":
    unittest.main()
