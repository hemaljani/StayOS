"""PULSE backend test suite.

Mirrors the ``pulse`` package layout: ``tests/common``, ``tests/rule_engine``,
``tests/triage``, ``tests/escalation``, ``tests/delivery``,
``tests/action_executor``, ``tests/demo_simulator``, ``tests/api``. Property
tests use Hypothesis; example/edge cases use plain pytest. Bedrock, DynamoDB,
and Web Push are mocked (moto / mocks) so tests run without live AWS.
"""
