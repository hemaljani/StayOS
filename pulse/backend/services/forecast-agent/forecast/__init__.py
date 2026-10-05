"""Forecast domain package for the PULSE Forecasting Agent.

Groups the agent's importable business logic, kept separate from the thin
``app`` entrypoint so it is unit/property tested without an AgentCore Runtime
session (PYQUALITY-05). Planned modules:

- ``config``: env-var loading (horizon, thresholds, table, tools, model id).
- ``invocation``: ``validate_invocation`` propertyId gate (Req 1.4, 1.5).
- ``signals``: Signal Reader over the shared Gateway MCP client + property
  isolation filter (Req 2, 6).
- ``engine``: the PURE Forecast Engine - deterministic oversell classification,
  rooms-oversold, and integer 0-100 confidence (Req 3.1-3.5). No I/O, no
  clients, no model.
- ``narrative``: Remediation Plan Author - AI-authors the walk plan around the
  engine's fixed numbers, with a deterministic template fallback (Req 3.7-3.10).
- ``alerts``: Alert Writer - dedupeKey derivation, create/update-in-place,
  resolve, and best-effort delivery (Req 4, 7).
"""
