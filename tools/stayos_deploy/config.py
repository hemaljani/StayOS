from dataclasses import dataclass
import os
from typing import Mapping, Optional


DEFAULT_REGION = "us-east-1"
DEFAULT_ENVIRONMENT = "test"


def _is_valid_account_id(account_id: str) -> bool:
    """Return True for a 12-character AWS account ID.

    Accepts the literal placeholder ``x`` as a mask character so that example
    and test configurations can use ``xxxxxxxxxxxx`` in place of a real account
    ID without weakening validation of actual 12-digit numeric IDs.
    """
    return len(account_id) == 12 and all(
        char.isdigit() or char == "x" for char in account_id
    )


def _value(
    explicit: Optional[str],
    environ: Mapping[str, str],
    *names: str,
    default: str = ""
) -> str:
    if explicit:
        return explicit
    for name in names:
        value = environ.get(name, "").strip()
        if value:
            return value
    return default


@dataclass(frozen=True)
class DeploymentConfig:
    environment: str
    profile: str
    cloudformation_profile: str
    region: str
    expected_account_id: str
    lumi_stack_prefix: str
    pulse_stack_prefix: str
    data_stack_prefix: str

    @classmethod
    def load(
        cls,
        *,
        environment: Optional[str] = None,
        profile: Optional[str] = None,
        cloudformation_profile: Optional[str] = None,
        region: Optional[str] = None,
        expected_account_id: Optional[str] = None,
        lumi_stack_prefix: Optional[str] = None,
        pulse_stack_prefix: Optional[str] = None,
        data_stack_prefix: Optional[str] = None,
        environ: Optional[Mapping[str, str]] = None
    ) -> "DeploymentConfig":
        values = os.environ if environ is None else environ
        resolved_profile = _value(profile, values, "PROFILE", "AWS_PROFILE")
        config = cls(
            environment=_value(
                environment,
                values,
                "ENVIRONMENT",
                default=DEFAULT_ENVIRONMENT
            ),
            profile=resolved_profile,
            cloudformation_profile=_value(
                cloudformation_profile,
                values,
                "CLOUDFORMATION_PROFILE",
                default=resolved_profile,
            ),
            region=_value(
                region,
                values,
                "REGION",
                "AWS_REGION",
                "AWS_DEFAULT_REGION",
                default=DEFAULT_REGION
            ),
            expected_account_id=_value(
                expected_account_id,
                values,
                "EXPECTED_ACCOUNT_ID"
            ),
            lumi_stack_prefix=_value(
                lumi_stack_prefix,
                values,
                "LUMI_STACK_PREFIX",
                default="stayos"
            ),
            pulse_stack_prefix=_value(
                pulse_stack_prefix,
                values,
                "PULSE_STACK_PREFIX",
                default="pulse"
            ),
            data_stack_prefix=_value(
                data_stack_prefix,
                values,
                "DATA_STACK_PREFIX",
                default="stayos-data"
            ),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not self.environment.replace("-", "").replace("_", "").isalnum():
            raise ValueError(
                "environment must contain only letters, numbers, '-' or '_'"
            )
        if not self.region:
            raise ValueError("region is required")
        if self.expected_account_id and not _is_valid_account_id(
            self.expected_account_id
        ):
            raise ValueError("expected account ID must be exactly 12 digits")

    @property
    def lumi_stack(self) -> str:
        return "{}-{}".format(self.lumi_stack_prefix, self.region)

    @property
    def pulse_stack(self) -> str:
        return "{}-{}".format(self.pulse_stack_prefix, self.region)

    @property
    def data_stack(self) -> str:
        return "{}-{}".format(self.data_stack_prefix, self.region)
