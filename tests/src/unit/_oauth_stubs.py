"""Minimal Home Assistant module stubs for OAuth unit tests."""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any


class HomeAssistantView:
    """Subclassable stand-in for Home Assistant's HTTP view base."""

    requires_auth = True
    cors_allowed = False
    url: str | None = None
    name: str | None = None


class HomeAssistant:
    """Typing stand-in used only by imported annotations."""


def install() -> None:
    """Install only the Home Assistant modules imported by the OAuth surface."""
    if "homeassistant.components.http" in sys.modules:
        return

    homeassistant = ModuleType("homeassistant")
    homeassistant.__path__ = []
    components = ModuleType("homeassistant.components")
    components.__path__ = []
    http = ModuleType("homeassistant.components.http")
    webhook = ModuleType("homeassistant.components.webhook")
    core = ModuleType("homeassistant.core")

    http.HomeAssistantView = HomeAssistantView
    core.HomeAssistant = HomeAssistant

    def async_register(*_args: Any, **_kwargs: Any) -> None:
        return None

    def async_unregister(*_args: Any, **_kwargs: Any) -> None:
        return None

    webhook.async_register = async_register
    webhook.async_unregister = async_unregister

    modules = {
        "homeassistant": homeassistant,
        "homeassistant.components": components,
        "homeassistant.components.http": http,
        "homeassistant.components.webhook": webhook,
        "homeassistant.core": core,
    }
    sys.modules.update(modules)
