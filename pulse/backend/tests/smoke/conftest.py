"""Fixtures and a CloudFormation-aware YAML loader for the smoke tests.

CloudFormation templates use intrinsic-function tags (``!Ref``, ``!Sub``,
``!GetAtt``, ``!Not``, ``!Equals``, ...) that a plain ``yaml.safe_load`` cannot
parse. This module provides a permissive loader that represents each tagged
node as a passthrough ``{"<Fn>": <value>}`` mapping, so the templates parse into
inspectable Python structures while the intrinsic references stay visible for
assertions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

# Repository layout anchored on this file:
#   .../pulse/backend/tests/smoke/conftest.py
#   parents[2] = backend, parents[3] = pulse, parents[4] = repo root (StayOS)
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_PULSE_DIR = Path(__file__).resolve().parents[3]
_REPO_ROOT = Path(__file__).resolve().parents[4]

_PULSE_NESTED = _PULSE_DIR / "infrastructure" / "nested-stacks"
_LUMI_NESTED = _REPO_ROOT / "lumi" / "infrastructure" / "nested-stacks"

PULSE_DATA_TEMPLATE = _PULSE_NESTED / "pulse-data.yaml"
PULSE_API_TEMPLATE = _PULSE_NESTED / "pulse-api.yaml"
PULSE_PIPELINE_TEMPLATE = _PULSE_NESTED / "pulse-pipeline.yaml"
PULSE_OBSERVABILITY_TEMPLATE = _PULSE_NESTED / "pulse-observability.yaml"
LUMI_DATA_TEMPLATE = _LUMI_NESTED / "data.yaml"


class CfnLoader(yaml.SafeLoader):
    """A YAML SafeLoader that tolerates CloudFormation intrinsic-function tags."""


def _construct_cfn_tag(loader: CfnLoader, tag_suffix: str, node: Any) -> Any:
    """Represent a ``!<Fn>`` tagged node as a ``{"<Fn>": value}`` passthrough.

    Args:
        loader: The active YAML loader.
        tag_suffix: The intrinsic function name (e.g. ``Ref``, ``Sub``).
        node: The tagged YAML node.

    Returns:
        A mapping keyed by the intrinsic name with the node's value, so the
        template structure and its references remain inspectable.
    """
    if isinstance(node, yaml.ScalarNode):
        value: Any = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {tag_suffix: value}


# Match every short-form ("!Ref") and long-form ("!Fn::GetAtt") intrinsic tag.
CfnLoader.add_multi_constructor("!", _construct_cfn_tag)


def load_cfn_template(path: Path) -> dict[str, Any]:
    """Parse a CloudFormation template with the permissive loader.

    Args:
        path: The template file path.

    Returns:
        The parsed template as a dict.
    """
    with path.open("r", encoding="utf-8") as handle:
        return yaml.load(handle, Loader=CfnLoader)  # noqa: S506 - CfnLoader is SafeLoader-based


@pytest.fixture(scope="session")
def pulse_data_template() -> dict[str, Any]:
    """Return the parsed ``pulse-data`` nested-stack template."""
    return load_cfn_template(PULSE_DATA_TEMPLATE)


@pytest.fixture(scope="session")
def pulse_api_template() -> dict[str, Any]:
    """Return the parsed ``pulse-api`` nested-stack template."""
    return load_cfn_template(PULSE_API_TEMPLATE)


@pytest.fixture(scope="session")
def pulse_pipeline_template() -> dict[str, Any]:
    """Return the parsed ``pulse-pipeline`` nested-stack template."""
    return load_cfn_template(PULSE_PIPELINE_TEMPLATE)


@pytest.fixture(scope="session")
def pulse_observability_template() -> dict[str, Any]:
    """Return the parsed ``pulse-observability`` nested-stack template."""
    return load_cfn_template(PULSE_OBSERVABILITY_TEMPLATE)


@pytest.fixture(scope="session")
def lumi_data_template() -> dict[str, Any]:
    """Return the parsed LUMI ``data`` nested-stack template."""
    return load_cfn_template(LUMI_DATA_TEMPLATE)
