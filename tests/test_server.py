"""The listener has to exist on every address family the machine can use.

The bug these tests guard against is invisible on the Pi itself: the server runs, the
UI loads on ``localhost``, and nothing outside can reach it because the wildcard bind
covered only IPv4 while the reachable path from the client was IPv6. See
docs/HARDWARE.md for the wiring that produced it.
"""

from __future__ import annotations

import socket

import pytest

from arducam_ptz import server


def _ipv6_usable() -> bool:
    """True if this machine can bind an IPv6 socket at all (some CI hosts cannot)."""
    try:
        probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    except OSError:
        return False
    try:
        probe.bind(("::1", 0))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def test_wildcard_binds_both_address_families():
    """A wildcard host must produce an IPv4 *and* (where possible) an IPv6 listener."""
    sockets = server.bind_sockets("0.0.0.0", 0)
    try:
        families = [sock.family for sock in sockets]
        assert socket.AF_INET in families
        if _ipv6_usable():
            assert socket.AF_INET6 in families, "wildcard bind left IPv6 unserved"
        else:
            assert families == [socket.AF_INET], "only the IPv4 socket is possible here"
    finally:
        for sock in sockets:
            sock.close()


def test_the_ipv6_socket_is_v6only():
    """Each family gets its own socket, so behaviour cannot depend on bindv6only.

    A dual-stack ``::`` socket behaves differently depending on a kernel sysctl; two
    sockets with ``IPV6_V6ONLY`` set behave the same on every machine.
    """
    if not _ipv6_usable():
        pytest.skip("no IPv6 stack on this host")
    sockets = server.bind_sockets("::", 0)
    try:
        v6 = next(sock for sock in sockets if sock.family == socket.AF_INET6)
        assert v6.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY) == 1
    finally:
        for sock in sockets:
            sock.close()


def test_sockets_are_reusable_but_not_sharable():
    """SO_REUSEADDR yes (restart over TIME_WAIT), SO_REUSEPORT no (no port stealing)."""
    sockets = server.bind_sockets("127.0.0.1", 0)
    try:
        assert len(sockets) == 1
        sock = sockets[0]
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) == 1
        if hasattr(socket, "SO_REUSEPORT"):
            assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT) == 0
    finally:
        for sock in sockets:
            sock.close()


def test_named_address_binds_only_that_address():
    """A specific host keeps the old meaning: one socket, bound where it was asked for."""
    sockets = server.bind_sockets("127.0.0.1", 0)
    try:
        assert len(sockets) == 1
        assert sockets[0].getsockname()[0] == "127.0.0.1"
    finally:
        for sock in sockets:
            sock.close()


def test_a_taken_port_is_reported_not_swallowed():
    """Failing to bind must say so - a silent exit is how the original bug hid."""
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    try:
        taken = holder.getsockname()[1]
        with pytest.raises(SystemExit) as excinfo:
            server.bind_sockets("127.0.0.1", taken)
        assert "could not bind" in str(excinfo.value)
        assert "in use" in str(excinfo.value).lower()
    finally:
        holder.close()


def test_loopback_interface_can_actually_be_connected_to():
    """Not just bound: accept a real connection on the socket we hand to uvicorn."""
    sockets = server.bind_sockets("127.0.0.1", 0)
    try:
        sock = sockets[0]
        sock.listen(1)  # already listening; repeated listen() is harmless
        client = socket.create_connection(sock.getsockname(), timeout=5)
        try:
            accepted, _ = sock.accept()
            accepted.close()
        finally:
            client.close()
    finally:
        for sock in sockets:
            sock.close()


def test_describe_sockets_prints_what_a_client_can_type():
    """The startup log must show reachable URLs, one per family."""
    sockets = server.bind_sockets("127.0.0.1", 0)
    try:
        described = server.describe_sockets(sockets)
        assert described[0].startswith("http://127.0.0.1:")
        assert described[0].endswith("(IPv4)")
    finally:
        for sock in sockets:
            sock.close()

    if _ipv6_usable():
        sockets = server.bind_sockets("::", 0)
        try:
            described = server.describe_sockets(sockets)
            addresses = {
                sock.family: text for sock, text in zip(sockets, described, strict=True)
            }
            assert addresses[socket.AF_INET6].startswith("http://[::]:")
            assert addresses[socket.AF_INET6].endswith("(IPv6)")
        finally:
            for sock in sockets:
                sock.close()
