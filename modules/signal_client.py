"""
Signal Client - JSON-RPC communication with signal-cli daemon
"""

import json
import socket
import time
import logging
from typing import Optional, List

logger = logging.getLogger(__name__)


class SignalClient:
    """JSON-RPC client for signal-cli daemon"""

    def __init__(self, config: dict):
        self.host = config.get("daemon_host", "127.0.0.1")
        self.port = config.get("daemon_port", 7583)
        self.timeout = config.get("daemon_timeout", 10)

    def send_jsonrpc(self, method: str, params: dict) -> Optional[dict]:
        """Send JSON-RPC request to signal-cli daemon"""
        try:
            request = {
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
                "id": str(int(time.time() * 1000))
            }

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(self.timeout)
                s.connect((self.host, self.port))
                s.sendall(json.dumps(request).encode() + b'\n')

                response = b''
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    response += chunk
                    if b'\n' in chunk:
                        break

                if response:
                    return json.loads(response.decode())

        except Exception:
            logger.exception(f"JSON-RPC error for {method}:")
        return None

    def send_message(
        self,
        group_id: str,
        message: str,
        quote_timestamp: Optional[int] = None,
        quote_author: Optional[str] = None
    ) -> bool:
        """Send a message to a group"""
        params = {
            "groupId": group_id,
            "message": message
        }

        if quote_timestamp and quote_author:
            params["quote-timestamp"] = quote_timestamp
            params["quote-author"] = quote_author

        result = self.send_jsonrpc("send", params)
        if result and "result" in result:
            logger.info("Sent message")
            return True
        else:
            logger.error(f"Failed to send message: {result}")
            return False

    def send_poll(
        self,
        group_id: str,
        question: str,
        options: List[str]
    ) -> bool:
        """Send a poll to a group"""
        params = {
            "groupId": group_id,
            "question": question,
            "options": options
        }

        result = self.send_jsonrpc("sendPollCreate", params)
        if result and "result" in result:
            logger.info(f"Sent poll: {question}")
            return True
        elif result and "error" in result:
            logger.error(f"Failed to send poll: {result['error']}")
            return False
        else:
            logger.error(f"Failed to send poll: {result}")
            return False

    def create_subscription_socket(self) -> socket.socket:
        """Create a socket subscribed to incoming messages"""
        request = {
            "jsonrpc": "2.0",
            "method": "subscribeReceive",
            "params": {},
            "id": "subscribe"
        }

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((self.host, self.port))
        sock.sendall(json.dumps(request).encode() + b'\n')
        logger.info("Connected to signal-cli daemon")
        return sock
