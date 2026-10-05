"""Integration tests for PULSE (Task 23.1-23.3).

These tests wire multiple PULSE components together against a moto-backed
DynamoDB and mocked Bedrock / AppSync Events / Gateway / Web Push seams. They
exercise the real in-process logic end to end (rule evaluation, persistence,
delivery, the closed-loop write-back, INFO batching, history, observability
emission, and OnSubscribe property scoping). Assertions that genuinely require
live AWS (real DynamoDB Streams propagation timing, a real Cognito-authorized
AppSync WebSocket client) are exercised against the equivalent in-process logic
and the live-only portion is marked skip-with-reason so the suite stays green.
"""
