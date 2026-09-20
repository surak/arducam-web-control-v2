"""HTTP API: thin wrappers over the services, plus the wire contract."""

from __future__ import annotations

from arducam_ptz.api.app import API_PREFIX, create_app

__all__ = ["API_PREFIX", "create_app"]
