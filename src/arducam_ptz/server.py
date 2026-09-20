"""Run the HTTP server (``arducam-server`` or ``arducam serve``).

Kept separate from the CLI client on purpose: the client only speaks HTTP and can run
on a laptop against the Pi, while this module is what actually runs on the Pi. A
systemd unit (or launchd plist) points at this entry point.

Address families
----------------
A *wildcard* host (``0.0.0.0`` or ``::``) is bound on **both** IPv4 and IPv6, with one
listening socket per family. That is not fussiness. On the network this rig lives on,
IPv4 packets from the client machine never arrive at the Pi - sshd listens on IPv4 and
on IPv6, and only the IPv6 path works (see docs/HARDWARE.md). A wildcard bind that
covers one family therefore serves the UI to the Pi itself and to nobody else, which
is exactly how the bug was reported: "it only connects from localhost".

Two sockets rather than a single dual-stack ``::`` socket, because a v6 socket that
also accepts IPv4 depends on ``net.ipv6.bindv6only`` being 0 - a kernel setting that
varies between machines. The IPv6 socket here is opened with ``IPV6_V6ONLY`` so each
family is served by its own socket and the behaviour is the same everywhere.
"""

from __future__ import annotations

import argparse
import logging
import socket
import sys
from pathlib import Path

from arducam_ptz.config import load_settings

logger = logging.getLogger(__name__)

#: Host values that mean "every interface this machine has". ``::`` is in the set so a
#: config written for an IPv6-only setup still gets its IPv4 sibling for free.
WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", "*", ""})

#: Backlog for the listening sockets. uvicorn's own default is 2048; the value only
#: matters for connection bursts, and asyncio calls ``listen()`` again on startup.
_LISTEN_BACKLOG = 2048


def _bind(family: int, address: tuple, *, v6only: bool = False) -> socket.socket:
    """Create, configure and bind one listening socket.

    Raises :class:`OSError` if the address cannot be bound - port already in use, no
    such address, or no usable IPv6 stack on this machine.
    """
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        # SO_REUSEADDR so a restart can rebind while old connections are still in
        # TIME_WAIT. It does *not* let a second live process steal the port - that
        # would need SO_REUSEPORT, which is deliberately left unset.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6 and v6only:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.bind(address)
        sock.listen(_LISTEN_BACKLOG)
    except BaseException:
        sock.close()
        raise
    return sock


def bind_sockets(host: str, port: int) -> list[socket.socket]:
    """Bind the listening socket(s) for ``host``:``port``.

    A wildcard ``host`` yields two sockets (IPv4 *and* IPv6) - unless this machine has
    no usable IPv6 stack, in which case the IPv4 one is returned alone, because a box
    without IPv6 must still serve its UI. Any other host is resolved with
    :func:`socket.getaddrinfo` and the first address that accepts a bind wins, so
    ``localhost``, a specific interface address or an IPv6 literal behaves exactly as
    it does with any other server.

    Callers pass a real port; port 0 (tests) lets each socket choose its own free port,
    so a wildcard bind then returns sockets on *different* ports.

    Raises :class:`SystemExit` when nothing could be bound, listing every address that
    was tried and why it failed - a server that exits silently is how the original
    "the page does not load" report became a mystery.
    """
    if host in WILDCARD_HOSTS:
        candidates: list[tuple[int, tuple, bool]] = [
            (socket.AF_INET6, ("::", port), True),
            (socket.AF_INET, ("0.0.0.0", port), False),
        ]
    else:
        candidates = []
        seen: set[tuple[int, tuple]] = set()
        for family, _type, _proto, _canon, sockaddr in socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        ):
            key = (family, sockaddr[:2])
            if key in seen:  # getaddrinfo happily repeats itself
                continue
            seen.add(key)
            candidates.append((family, sockaddr, family == socket.AF_INET6))

    sockets: list[socket.socket] = []
    errors: list[str] = []
    for family, address, v6only in candidates:
        try:
            sockets.append(_bind(family, address, v6only=v6only))
        except OSError as exc:
            errors.append(f"{address[0]}:{address[1]} ({exc.strerror or exc})")
            continue
        if host not in WILDCARD_HOSTS:
            break  # a named address: the first one that binds is the answer
    if not sockets:
        raise SystemExit(
            f"could not bind any listening socket for {host}:{port}: " + "; ".join(errors)
        )
    return sockets


def describe_sockets(sockets: list[socket.socket]) -> list[str]:
    """Return a ``url (family)`` string per socket, for the startup log.

    These are the addresses a client can actually type, which is the whole point: the
    bug being fixed here was invisible until somebody read the listener list and saw
    that only one family had one.
    """
    described = []
    for sock in sockets:
        family = "IPv6" if sock.family == socket.AF_INET6 else "IPv4"
        host, port = sock.getsockname()[:2]
        shown = f"[{host}]" if sock.family == socket.AF_INET6 else host
        described.append(f"http://{shown}:{port} ({family})")
    return described


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
    parser.add_argument(
        "--host",
        default=None,
        help=(
            "Override the configured listen address. A wildcard (0.0.0.0 or ::) binds "
            "both IPv4 and IPv6; name a specific address (e.g. 127.0.0.1) to bind only "
            "that one"
        ),
    )
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
    app = create_app(settings)

    if args.reload:
        # Development only. uvicorn's reloader insists on an import string and manages
        # its own single-family socket, so the bind below does not apply on this path.
        logger.warning(
            "serving http://%s:%d with --reload: single-family bind, development use only",
            settings.server.host,
            settings.server.port,
        )
        uvicorn.run(
            app,
            host=settings.server.host,
            port=settings.server.port,
            log_level=settings.server.log_level,
            reload=True,
        )
        return 0

    sockets = bind_sockets(settings.server.host, settings.server.port)
    for where in describe_sockets(sockets):
        logger.info("serving %s (simulated=%s)", where, settings.simulate_everything())

    config = uvicorn.Config(
        app,
        host=settings.server.host,
        port=settings.server.port,
        log_level=settings.server.log_level,
    )
    uvicorn.Server(config).run(sockets=sockets)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
