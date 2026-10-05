"""C3b VIPs/Ops read facade (``pulse-ops-read``) for the PULSE PWA.

This package is a dedicated read-only facade Lambda that is an MCP client to the
shared StayOS AgentCore Gateway (IAM auth, ``bedrock-agentcore:InvokeGateway``).
It serves two PULSE REST routes and shapes the shared-Gateway tool results for
the PWA's VIPs and Ops tabs, so the PWA stays single-origin against the PULSE
API (Component 5, Decision 9):

    * ``GET /vips?propertyId=`` -> VIP arrivals grouped by loyalty tier, with
      each guest's profile fields and preferences preserved (from
      ``get_vip_guests``).
    * ``GET /ops?propertyId=`` -> a facility summary (from ``get_occupancy``),
      out-of-order room cards annotated with their work-order status (from
      ``get_room_status`` joined with ``get_work_orders``), and a group-checkout
      summary (from ``get_occupancy``).

Every request is property-scoped SERVER-SIDE from the caller's Cognito claims:
the requested ``propertyId`` must be in the caller's associated set, which is
validated before any Gateway tool is called (Requirement 16.6).
"""

from __future__ import annotations

__all__: list[str] = []
