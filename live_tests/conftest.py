"""Live-test socket policy override.

Home Assistant's pytest plugin deliberately reapplies a localhost-only socket
policy in ``pytest_runtest_setup``.  The protected Bright contract is the one
place in this repository where real outbound network access is intentional.

Capture the real socket functions before Home Assistant patches them, then
restore them in a try-last setup hook for tests carrying the ``live`` marker.
Normal CI tests are unaffected.
"""
from __future__ import annotations

import socket

import pytest

_REAL_SOCKET = socket.socket
_REAL_CONNECT = socket.socket.connect
_REAL_GETADDRINFO = socket.getaddrinfo
_REAL_GETHOSTBYNAME = socket.gethostbyname
_REAL_GETHOSTBYNAME_EX = socket.gethostbyname_ex


@pytest.hookimpl(trylast=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    """Restore real sockets after Home Assistant applies its test restriction."""
    if item.get_closest_marker("live") is None:
        return

    socket.socket = _REAL_SOCKET
    socket.socket.connect = _REAL_CONNECT
    socket.getaddrinfo = _REAL_GETADDRINFO
    socket.gethostbyname = _REAL_GETHOSTBYNAME
    socket.gethostbyname_ex = _REAL_GETHOSTBYNAME_EX
