"""Demo Scenario Simulator component (``pulse-demo-simulator``).

Produces the operational-data changes that make PULSE observable in a demo by
applying scripted, deterministic mutations to the LUMI operational tables.
Demo-only; excluded from real deployments via the ``EnableDemoSimulator``
CloudFormation condition.

Implementation is added in later tasks; this module currently only marks the
sub-package.
"""

__all__: list[str] = []
