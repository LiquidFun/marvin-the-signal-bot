"""
Signal Client - JSON-RPC communication with signal-cli daemon

Failures are split by what they mean for a message that was being sent,
because that decides whether retrying can produce a duplicate:

- DaemonUnreachable: no connection was made, so nothing was sent.
- RpcError / NoResponse: the request reached the daemon. The message may or
  may not have gone out (signal-cli reports an error if a single group member
  is unreachable, after delivering to everyone else).
"""

import json
import logging
import socket
import time
import uuid
from typing import List, Optional

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 5


class SignalError(Exception):
    """Base class for everything that can go wrong talking to signal-cli"""


class DaemonUnreachable(SignalError):
    """Could not connect to the daemon. Nothing was sent; retrying is safe."""


class NoResponse(SignalError):
    """The request was sent, but no response came back."""


class RpcError(SignalError):
    """The daemon answered with a JSON-RPC error."""

    def __init__(self, method: str, error):
        if not isinstance(error, dict):
            error = {"message": str(error)}
        self.code = error.get("code")
        # For a failed send this may hold the per-recipient results
        self.data = error.get("data")
        super().__init__(f"{method}: {error.get('message', error)} (code {self.code})")


class SignalClient:
    """JSON-RPC client for signal-cli daemon"""

    def __init__(self, config: dict):
        self.host = config.get("daemon_host", "127.0.0.1")
        self.port = config.get("daemon_port", 7583)
        # Sending to a large group can take a while; giving up early turns a
        # delivered message into an unknown outcome.
        self.timeout = config.get("daemon_timeout", 120)

    def connect(self) -> socket.socket:
        try:
            sock = socket.create_connection((self.host, self.port), timeout=CONNECT_TIMEOUT)
        except OSError as e:
            raise DaemonUnreachable(f"{self.host}:{self.port}: {e}") from e
        sock.settimeout(self.timeout)
        return sock

    def call(self, method: str, params: Optional[dict] = None):
        """Send a JSON-RPC request and return its result.

        Reads lines until one matches our request id, skipping unsolicited
        notifications (e.g. delivery receipts) the daemon pushes to every
        connected client.
        """
        request_id = uuid.uuid4().hex
        request = {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": request_id}

        with self.connect() as sock:
            try:
                sock.sendall(json.dumps(request).encode() + b"\n")
                response = self._read_response(sock, request_id)
            except (OSError, ValueError) as e:
                raise NoResponse(f"{method}: {e}") from e

        if response is None:
            raise NoResponse(f"{method}: connection closed without a response")
        if "error" in response:
            raise RpcError(method, response["error"])
        return response.get("result")

    def _read_response(self, sock: socket.socket, request_id: str) -> Optional[dict]:
        # One deadline for the whole call: a stream of notifications must not
        # keep resetting a per-recv timeout while the response never comes.
        deadline = time.monotonic() + self.timeout
        buf = b""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no response within {self.timeout}s")
            sock.settimeout(remaining)
            chunk = sock.recv(65536)
            if not chunk:
                return None
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for line in lines:
                if not line.strip():
                    continue
                msg = json.loads(line)
                if isinstance(msg, dict) and msg.get("id") == request_id:
                    return msg
                logger.debug(f"Skipping unsolicited message: {str(msg)[:100]}")

    def send_message(
        self,
        group_id: str,
        message: str,
        quote_timestamp: Optional[int] = None,
        quote_author: Optional[str] = None
    ) -> dict:
        """Send a message to a group"""
        params = {"groupId": group_id, "message": message}
        if quote_timestamp and quote_author:
            params["quote-timestamp"] = quote_timestamp
            params["quote-author"] = quote_author
        return self.call("send", params)

    def send_poll(self, group_id: str, question: str, options: List[str]) -> dict:
        """Send a poll to a group. Members may pick several options."""
        params = {"groupId": group_id, "question": question, "options": options}
        return self.call("sendPollCreate", params)

    def list_groups(self) -> List[dict]:
        return self.call("listGroups")

    def subscribe(self) -> socket.socket:
        """Open a connection subscribed to incoming messages. Blocks on reads
        indefinitely: the daemon sends nothing while the group is quiet."""
        sock = self.connect()
        try:
            sock.settimeout(None)
            request = {"jsonrpc": "2.0", "method": "subscribeReceive", "params": {}, "id": "subscribe"}
            sock.sendall(json.dumps(request).encode() + b"\n")
        except BaseException:
            sock.close()
            raise
        logger.info("Connected to signal-cli daemon")
        return sock
