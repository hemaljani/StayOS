"""REST API component (``pulse-api`` behind API Gateway).

Hosts the thin router plus per-resource business-logic modules for alerts,
acknowledgements, resolutions, approvals, rules, shift handover, push
subscriptions, and realtime/VAPID config. All routes are Cognito-authenticated
and property-scoped from token claims.

Implementation is added in later tasks; this module currently only marks the
sub-package.
"""

__all__: list[str] = []
