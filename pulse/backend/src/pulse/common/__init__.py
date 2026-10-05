"""Shared PULSE building blocks used by every component.

This sub-package centralizes cross-cutting concerns so the individual Lambda
components (rule engine, triage, escalation, delivery, action executor, demo
simulator, api) never duplicate them:

- ``models``   Typed domain models and enums (the PULSE ubiquitous language).
- ``config``   Environment-variable configuration loader (no hardcoded names).
- ``logging``  AWS Lambda Powertools structured-logger factory.
- ``aws``      Module-level boto3 client factory with adaptive retries.
- ``dynamo``   DynamoDB resource/table helpers built on the client factory.
- ``errors``   Domain-specific exception hierarchy.
"""

__all__: list[str] = []
