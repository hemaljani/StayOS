"""PULSE Triage Agent AgentCore Runtime service.

Marks ``pulse/backend/services/triage-agent`` as a package. The service modules
(``server``, ``gateway``, ``situation``, ``narrative``, ``attach``, ``config``)
import one another flat (mirroring the LUMI chat agent), so at container start
``python server.py`` runs from this directory with the ``pulse`` package on the
path.
"""
