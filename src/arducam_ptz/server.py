"""Run the HTTP server (``arducam-server`` or ``arducam serve``).

Kept separate from the CLI client on purpose: the client only speaks HTTP and can run
on a laptop against the Pi, while this module is what actually runs on the Pi. A
systemd unit (or launchd plist) points at this entry point.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from arducam_ptz.config import load_settings

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arducam-server",
        description="Serve the arducam-ptz HTTP API and web UI.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="TOML config file (see config/arducam.example.toml)",
    )
    parser.add_argument("--host", default=None, help="Override the configured listen address")
    parser.add_argument("--port", type=int, default=None, help="Override the configured port")
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Run with simulated hardware: no camera, no I2C, no focus device",
    )
    parser.add_argument("--log-level", default=None, choices=["debug", "info", "warning", "error"])
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Auto-reload on code changes (development only)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, load settings, and hand off to uvicorn."""
    args = build_parser().parse_args(argv)
    settings = load_settings(args.config)
    if args.simulate:
        settings = settings.model_copy(update={"simulate": True})
    if args.host:
        settings = settings.with_server(host=args.host)
    if args.port:
        settings = settings.with_server(port=args.port)
    if args.log_level:
        settings = settings.with_server(log_level=args.log_level)

    # Imported here so ``--help`` works even if uvicorn is missing.
    import uvicorn

    from arducam_ptz.api.app import create_app

    logging.basicConfig(
        level=settings.server.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logger.info(
        "serving on http://%s:%d (simulated=%s)",
        settings.server.host,
        settings.server.port,
        settings.simulate_everything(),
    )
    uvicorn.run(
        create_app(settings),
        host=settings.server.host,
        port=settings.server.port,
        log_level=settings.server.log_level,
        reload=args.reload,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
