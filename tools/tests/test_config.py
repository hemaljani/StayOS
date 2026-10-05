import os
import sys
import unittest


TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

from stayos_deploy.config import DeploymentConfig  # noqa: E402


class DeploymentConfigTest(unittest.TestCase):
    def test_defaults_are_stable(self):
        config = DeploymentConfig.load(environ={})

        self.assertEqual("test", config.environment)
        self.assertEqual("", config.cloudformation_profile)
        self.assertEqual("us-east-1", config.region)
        self.assertEqual("stayos-us-east-1", config.lumi_stack)
        self.assertEqual("pulse-us-east-1", config.pulse_stack)
        self.assertEqual("stayos-data-us-east-1", config.data_stack)

    def test_canonical_profile_wins_over_aws_profile(self):
        config = DeploymentConfig.load(
            environ={"PROFILE": "canonical", "AWS_PROFILE": "legacy"}
        )

        self.assertEqual("canonical", config.profile)
        self.assertEqual("canonical", config.cloudformation_profile)

    def test_cloudformation_profile_can_differ_from_target_profile(self):
        config = DeploymentConfig.load(
            environ={
                "PROFILE": "target",
                "CLOUDFORMATION_PROFILE": "target-cfn",
            }
        )

        self.assertEqual("target", config.profile)
        self.assertEqual("target-cfn", config.cloudformation_profile)

    def test_explicit_values_win_over_environment(self):
        config = DeploymentConfig.load(
            environment="demo",
            region="us-west-2",
            expected_account_id="xxxxxxxxxxxx",
            environ={
                "ENVIRONMENT": "ignored",
                "REGION": "eu-west-1",
                "EXPECTED_ACCOUNT_ID": "111111111111",
            },
        )

        self.assertEqual("demo", config.environment)
        self.assertEqual("us-west-2", config.region)
        self.assertEqual("xxxxxxxxxxxx", config.expected_account_id)

    def test_rejects_invalid_account_id(self):
        with self.assertRaises(ValueError):
            DeploymentConfig.load(
                expected_account_id="1234",
                environ={},
            )


if __name__ == "__main__":
    unittest.main()
