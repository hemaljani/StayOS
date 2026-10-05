"""Voice Agent service package for StayOS.

This package implements a real-time conversational voice assistant using
Amazon Nova Sonic's bidirectional streaming, served via an aiohttp WebSocket
server running on ECS Fargate. The voice agent answers property-scoped
questions about hotel operations data (occupancy, revenue, VIPs, rooms,
work orders) using tool-augmented conversation.
"""
