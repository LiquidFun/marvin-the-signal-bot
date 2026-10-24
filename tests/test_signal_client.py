"""SignalClient against a fake daemon: response matching and the three kinds
of failure."""

import json
import socket
import threading
import time

import pytest

from modules.signal_client import DaemonUnreachable, NoResponse, RpcError, SignalClient


class FakeDaemon:
    """Minimal TCP server; `handler(conn, request)` writes the reply"""

    def __init__(self, handler):
        self.requests = []
        self._handler = handler
        self._server = socket.create_server(("127.0.0.1", 0))
        self.port = self._server.getsockname()[1]
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                break
            with conn:
                data = b""
                while b"\n" not in data and (chunk := conn.recv(4096)):
                    data += chunk
                request = json.loads(data)
                self.requests.append(request)
                self._handler(conn, request)

    def client(self, **config):
        return SignalClient({"daemon_port": self.port, **config})

    def close(self):
        self._server.shutdown(socket.SHUT_RDWR)  # wakes the blocked accept()
        self._server.close()
        self._thread.join(timeout=2)


@pytest.fixture
def daemon_with():
    daemons = []

    def start(handler):
        daemons.append(FakeDaemon(handler))
        return daemons[-1]

    yield start
    for daemon in daemons:
        daemon.close()


def reply(conn, msg):
    conn.sendall(json.dumps(msg).encode() + b"\n")


def notification(n=0):
    return {"jsonrpc": "2.0", "method": "receive", "params": {"envelope": {"n": n}}}


def test_skips_notifications_and_returns_result(daemon_with):
    """The daemon pushes receipts to every client; one arriving before the
    response used to be mistaken for a failed send, so the poll was re-posted."""
    def handler(conn, request):
        for n in range(3):
            reply(conn, notification(n))
        reply(conn, {"jsonrpc": "2.0", "id": request["id"], "result": {"timestamp": 7}})

    assert daemon_with(handler).client().call("send") == {"timestamp": 7}


def test_response_split_across_packets(daemon_with):
    def handler(conn, request):
        data = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": [1]}).encode() + b"\n"
        conn.sendall(data[:10])
        conn.sendall(data[10:])

    assert daemon_with(handler).client().call("listGroups") == [1]


def test_error_response_raises_rpc_error(daemon_with):
    def handler(conn, request):
        reply(conn, {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -1, "message": "nope"}})

    with pytest.raises(RpcError, match="nope") as excinfo:
        daemon_with(handler).client().call("send")
    assert excinfo.value.code == -1


def test_closed_without_response_raises_no_response(daemon_with):
    with pytest.raises(NoResponse):
        daemon_with(lambda conn, request: reply(conn, notification())).client().call("send")


def test_silent_daemon_times_out_as_no_response(daemon_with):
    stop = threading.Event()
    daemon = daemon_with(lambda conn, request: stop.wait(5))
    try:
        with pytest.raises(NoResponse):
            daemon.client(daemon_timeout=0.2).call("send")
    finally:
        stop.set()


def test_refused_connection_raises_daemon_unreachable():
    with socket.create_server(("127.0.0.1", 0)) as s:
        port = s.getsockname()[1]
    with pytest.raises(DaemonUnreachable):
        SignalClient({"daemon_port": port}).call("send")


def test_send_poll_request(daemon_with):
    def handler(conn, request):
        reply(conn, {"jsonrpc": "2.0", "id": request["id"], "result": {}})

    daemon = daemon_with(handler)
    daemon.client().send_poll("grp", "KW44", ["a", "b"])
    request = daemon.requests[0]
    assert request["method"] == "sendPollCreate"
    assert request["params"] == {"groupId": "grp", "question": "KW44", "options": ["a", "b"]}


def test_notification_stream_does_not_extend_the_deadline(daemon_with):
    stop = threading.Event()

    def handler(conn, request):
        while not stop.wait(0.05):
            reply(conn, notification())

    daemon = daemon_with(handler)
    start = time.monotonic()
    try:
        with pytest.raises(NoResponse):
            daemon.client(daemon_timeout=0.3).call("send")
    finally:
        stop.set()
    assert time.monotonic() - start < 1


def test_non_object_lines_are_skipped(daemon_with):
    def handler(conn, request):
        conn.sendall(b'[1, 2]\n"text"\nnull\n')
        reply(conn, {"jsonrpc": "2.0", "id": request["id"], "result": "ok"})

    assert daemon_with(handler).client().call("send") == "ok"


def test_error_that_is_not_an_object(daemon_with):
    def handler(conn, request):
        reply(conn, {"jsonrpc": "2.0", "id": request["id"], "error": "weird"})

    with pytest.raises(RpcError, match="weird"):
        daemon_with(handler).client().call("send")
