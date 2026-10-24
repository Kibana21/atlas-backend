"""A001-DEF-01 (E-48): readiness tells the truth after a Redis outage (FR-26, FR-56, AC-05).

An API process that holds a pooled Redis connection and sends nothing while Redis is unreachable must
report ``redis: ok`` on its **first** readiness check once Redis is back, and still report the outage
truthfully (503, within the 1 s probe budget) while it lasts.

The outage is simulated by a TCP pass-through proxy owned by this test, in front of the test Redis
(DB 11). The proxy drops every connection and stops listening; no real service is stopped or
reconfigured, and no key is written.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from collections.abc import Iterator
from urllib.parse import urlsplit, urlunsplit

import pytest
from fastapi.testclient import TestClient

from atlas.core.settings import Settings
from atlas.main import create_app

pytestmark = pytest.mark.integration

READY = "/api/v1/health/ready"


class FaultProxy:
    """Threaded TCP proxy on 127.0.0.1 that can drop its connections and stop/start listening."""

    def __init__(self, upstream: tuple[str, int]) -> None:
        self.upstream = upstream
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        self.port = int(probe.getsockname()[1])
        probe.close()
        self._listener: tuple[socket.socket, threading.Event, threading.Thread] | None = None
        self._sockets: set[socket.socket] = set()
        self._lock = threading.Lock()

    def start(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", self.port))
            listener.listen()
            # The accept loop polls a stop flag: on Linux, close() from another thread does not
            # interrupt a blocked accept(), so a closed-but-blocked listener would keep accepting.
            listener.settimeout(0.05)
        except OSError:
            listener.close()
            raise
        stopping = threading.Event()
        thread = threading.Thread(target=self._accept, args=(listener, stopping), daemon=True)
        thread.start()
        self._listener = (listener, stopping, thread)

    def stop(self) -> None:
        """Stop listening (new connections are refused) and drop every open connection."""
        current, self._listener = self._listener, None
        if current is not None:
            listener, stopping, thread = current
            stopping.set()
            thread.join(timeout=2)
            listener.close()
        with self._lock:
            sockets, self._sockets = self._sockets, set()
        for s in sockets:
            with contextlib.suppress(OSError):
                s.shutdown(socket.SHUT_RDWR)
            s.close()

    def _accept(self, listener: socket.socket, stopping: threading.Event) -> None:
        while not stopping.is_set():
            try:
                client, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            client.settimeout(None)
            try:
                upstream = socket.create_connection(self.upstream, timeout=2)
            except OSError:
                client.close()
                continue
            upstream.settimeout(None)
            with self._lock:
                self._sockets.update({client, upstream})
            for src, dst in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._pump, args=(src, dst), daemon=True).start()

    @staticmethod
    def _pump(src: socket.socket, dst: socket.socket) -> None:
        try:
            while data := src.recv(65536):
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                with contextlib.suppress(OSError):
                    s.shutdown(socket.SHUT_RDWR)


@pytest.fixture
def redis_proxy(settings: Settings) -> Iterator[tuple[FaultProxy, Settings]]:
    u = urlsplit(settings.redis_url)
    proxy = FaultProxy((u.hostname or "localhost", u.port or 6379))
    proxy.start()
    netloc = f"{u.username}:{u.password}@" if u.password else ""
    proxied = urlunsplit((u.scheme, f"{netloc}127.0.0.1:{proxy.port}", u.path, u.query, u.fragment))
    try:
        yield proxy, settings.model_copy(update={"redis_url": proxied})
    finally:
        proxy.stop()


def test_first_readiness_after_a_redis_outage_reports_redis_ok(redis_proxy: tuple[FaultProxy, Settings]) -> None:
    proxy, settings = redis_proxy
    with TestClient(create_app(settings)) as client:
        assert client.get(READY).status_code == 200  # a pooled Redis connection now exists
        proxy.stop()  # outage: the pooled connection is dropped while the app sends nothing
        time.sleep(0.2)
        proxy.start()  # Redis is reachable again
        r = client.get(READY)
    assert r.status_code == 200, r.json()
    assert r.json() == {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}


def test_readiness_during_a_redis_outage_is_truthful_and_bounded(redis_proxy: tuple[FaultProxy, Settings]) -> None:
    proxy, settings = redis_proxy
    with TestClient(create_app(settings)) as client:
        assert client.get(READY).status_code == 200
        proxy.stop()
        started = time.monotonic()
        r = client.get(READY)
        elapsed = time.monotonic() - started
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
        assert r.json()["error"]["details"] == {"failed_check": "redis"}
        assert str(proxy.port) not in r.text
        assert "127.0.0.1" not in r.text
        assert elapsed < 1.5, f"readiness took {elapsed:.2f} s during the outage (probe budget 1 s)"
        proxy.start()
        assert client.get(READY).status_code == 200  # and it recovers
