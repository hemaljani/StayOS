"""PULSE backend package (StayOS Feature #2).

PULSE is a real-time situational awareness layer that extends the existing
StayOS Progressive Web App. This top-level package hosts the serverless
Python components that make up the closed-loop alert pipeline:

- ``common``          Shared domain models, config, logging, AWS helpers, errors.
- ``rule_engine``     Consumes DynamoDB Streams and evaluates alert rules.
- ``triage``          Amazon Bedrock triage agent producing ranked options.
- ``escalation``      Escalation-trigger hierarchy and time-based routing chain.
- ``delivery``        Realtime publish (AppSync Events) + Web Push delivery.
- ``action_executor`` GM-approved write-backs that close the operational loop.
- ``demo_simulator``  Deterministic operational-data mutations for demos.
- ``api``             REST API business logic behind API Gateway.

All modules follow the project-wide PYQUALITY and NAMING conventions:
complete type hints, Google-style docstrings, Powertools structured logging,
and resource identifiers sourced exclusively from environment variables.
"""

__all__: list[str] = []

# Single source of truth for the PULSE backend version, surfaced in logs and
# health responses. Kept in sync with the packaging metadata in pyproject.toml.
__version__ = "0.1.0"
