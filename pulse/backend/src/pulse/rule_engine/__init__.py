"""Rule Engine component (``pulse-rule-evaluator``).

Consumes DynamoDB Stream events from the LUMI operational tables, evaluates
them against enabled rule definitions, and creates Alert records. The handler
is a thin orchestrator that delegates to pure, unit-testable business logic.

Implementation is added in later tasks; this module currently only marks the
sub-package.
"""

__all__: list[str] = []
