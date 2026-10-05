import os
import sys
import unittest


TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

from stayos_deploy.cli import create_plan  # noqa: E402
from stayos_deploy.config import DeploymentConfig  # noqa: E402


class FakeReader:
    def __init__(self, account_id):
        self._account_id = account_id

    def account_id(self):
        return self._account_id

    def stack_status(self, stack_name):
        return "{}_STATUS".format(stack_name)


class PlanTest(unittest.TestCase):
    def test_plan_is_read_only_and_reports_stages(self):
        config = DeploymentConfig.load(
            expected_account_id="xxxxxxxxxxxx",
            environ={},
        )

        plan = create_plan(config, FakeReader("xxxxxxxxxxxx"))

        self.assertFalse(plan["changesPlanned"])
        self.assertEqual("xxxxxxxxxxxx", plan["accountId"])
        self.assertEqual("xxxxxxxxxxxx", plan["cloudformationAccountId"])
        self.assertEqual(8, len(plan["stages"]))
        self.assertIn("aws", plan["tooling"])

    def test_plan_rejects_wrong_account(self):
        config = DeploymentConfig.load(
            expected_account_id="xxxxxxxxxxxx",
            environ={},
        )

        with self.assertRaises(RuntimeError):
            create_plan(config, FakeReader("111111111111"))

    def test_plan_rejects_cloudformation_profile_in_another_account(self):
        config = DeploymentConfig.load(
            expected_account_id="xxxxxxxxxxxx",
            cloudformation_profile="target-cfn",
            environ={},
        )

        with self.assertRaises(RuntimeError):
            create_plan(
                config,
                FakeReader("xxxxxxxxxxxx"),
                FakeReader("yyyyyyyyyyyy"),
            )


if __name__ == "__main__":
    unittest.main()
