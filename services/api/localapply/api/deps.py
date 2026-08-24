"""Shared FastAPI dependencies."""

from __future__ import annotations

from fastapi import Request

from ..config import Settings
from ..events.bus import EventBus
from ..orchestrator.run_loop import RunManager


def get_run_manager(request: Request) -> RunManager:
    return request.app.state.runs


def get_bus(request: Request) -> EventBus:
    return request.app.state.bus


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings
