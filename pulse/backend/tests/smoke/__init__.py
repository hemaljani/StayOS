"""Smoke tests for PULSE one-time configuration (Task 23.4).

These are single-execution assertions over the CloudFormation nested-stack
templates and the seeded default rule templates. They confirm the static
configuration the closed loop depends on: the ``pulse-rules`` key schema, the
default rule-template seeding, the LUMI operational-table Streams enablement,
the observability dashboard widgets and alarms, the ``pulse-alert-history`` TTL,
the REST Cognito JWT authorizer wiring, and the AppSync Events realtime config.
The templates are parsed with a permissive loader that tolerates CloudFormation
intrinsic-function tags (``!Ref``, ``!Sub``, ``!GetAtt``, ...).
"""
