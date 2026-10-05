"""PULSE Forecasting Agent AgentCore Runtime service.

Marks ``pulse/backend/services/forecast-agent`` as a package. Mirrors the PULSE
Triage Agent one-for-one: a thin Strands agent entrypoint (``app``) delegates to
the importable ``forecast`` domain package (``forecast.engine``,
``forecast.signals``, ``forecast.narrative``, ``forecast.alerts``,
``forecast.invocation``, ``forecast.config``). At container start
``python app.py`` runs from this directory with the ``pulse`` package on the
path.

The design invariant of the feature lives here: detection (oversell count,
anticipated date, integer 0-100 confidence) is computed by the PURE
``forecast.engine`` module and is never model-generated; the Remediation_Plan is
AI-authored by ``forecast.narrative`` around those fixed numbers.
"""
