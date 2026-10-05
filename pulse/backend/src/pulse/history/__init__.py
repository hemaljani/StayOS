"""Alert history and shift-handover (``pulse-alert-history``).

Appends a versioned record to ``pulse-alert-history`` on every ``pulse-alerts``
create/status-change (the shift-handover source, Requirement 14), and serves the
shift-handover window query. Writing is triggered by the ``pulse-alerts``
DynamoDB Stream; the query backs the ``GET /shift-handover`` API route.
"""

__all__: list[str] = []
