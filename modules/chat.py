"""
Chat Handler - AI-powered chat responses for Signal groups
"""

import json
import os
import time
import logging
from typing import Set, Optional, List, Dict
from collections import deque
from datetime import datetime

from .signal_client import DaemonUnreachable, SignalClient, SignalError

logger = logging.getLogger(__name__)

RECONNECT_MIN = 5
RECONNECT_MAX = 60


class ChatHandler:
    """Handles AI chat responses in Signal groups"""

    def __init__(self, signal_client: SignalClient, config: dict):
        self.client = signal_client
        self.config = config
        self.group_id = config.get("group_id")
        self.bot_number = config.get("bot_number")
        self.system_prompt = config.get("system_prompt", "")

        self.responded_messages: Set[str] = set()
        self.message_history: deque = deque(maxlen=config.get("context_messages", 15))
        self.history_file = config.get("history_file", "message_history.json")
        self.load_history()

    def load_history(self):
        """Load message history from disk"""
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, 'r') as f:
                    data = json.load(f)
                    for msg in data.get("messages", []):
                        self.message_history.append(msg)
                    logger.info(f"Loaded {len(self.message_history)} messages from history")
            except Exception:
                logger.exception("Failed to load history:")

    def save_history(self):
        """Save message history to disk"""
        try:
            data = {
                "messages": list(self.message_history),
                "last_updated": datetime.now().isoformat()
            }
            with open(self.history_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception:
            logger.exception("Failed to save history:")

    def get_llm_response(self, user_message: str, context: List[Dict]) -> str:
        """Get response from LLM with conversation context"""
        try:
            import requests

            context_text = ""
            if context:
                context_text = "Letzte Nachrichten im Chat:\n"
                for msg in context:
                    sender = msg.get("sender", "Unknown")
                    content = msg.get("content", "")
                    timestamp = msg.get("timestamp", "")
                    context_text += f"[{timestamp}] {sender}: {content}\n"
                context_text += "\n---\n"

            full_prompt = f"{context_text}Darauf sollst du jetzt antworten: {user_message}"

            payload = {
                "model": self.config["model"],
                "messages": [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": full_prompt}
                ],
                "temperature": self.config.get("temperature", 0.8),
                "max_tokens": self.config.get("max_tokens", 200)
            }

            # Generous: the LLM runs on another machine and may have to load
            # the model into VRAM first.
            response = requests.post(
                self.config["llm_url"], json=payload, timeout=self.config.get("llm_timeout", 180)
            )
            response.raise_for_status()
            result = response.json()
            return result["choices"][0]["message"]["content"].strip()

        except Exception:
            logger.exception("LLM error:")
            return "Hier stehe ich, mit einem Gehirn von der Groesse eines Planeten, und ich kann nicht einmal eine Antwort generieren. Die Sinnlosigkeit ist ueberwältigend."

    def add_to_history(self, sender: str, message: str, timestamp: int):
        """Add message to conversation history"""
        dt = datetime.fromtimestamp(timestamp / 1000)
        time_str = dt.strftime("%H:%M")

        self.message_history.append({
            "sender": sender,
            "content": message,
            "timestamp": time_str
        })

        self.save_history()

    def is_bot_mentioned(self, message_data: dict) -> bool:
        """Check if bot was mentioned in the message"""
        mentions = message_data.get("mentions", [])
        if not mentions:
            return False

        for mention in mentions:
            if mention.get("number") == self.bot_number:
                return True
        return False

    def get_sender_name(self, envelope: dict) -> str:
        """Extract sender name from envelope"""
        source_name = envelope.get("sourceName")
        if source_name:
            return source_name
        source_number = envelope.get("source") or envelope.get("sourceNumber")
        return source_number if source_number else "Unknown"

    def process_message(self, envelope: dict):
        """Process incoming message"""
        try:
            data_message = envelope.get("dataMessage", {})
            if not data_message:
                return

            group_info = data_message.get("groupInfo", {})
            group_id = group_info.get("groupId")

            if group_id != self.group_id:
                return

            timestamp = envelope.get("timestamp", 0)
            sender = envelope.get("source") or envelope.get("sourceNumber")
            message_id = f"{sender}_{timestamp}"

            message_text = (data_message.get("message") or "").strip()
            message_text = message_text.replace("\ufffc ", "")
            sender_name = self.get_sender_name(envelope)

            logger.info(f"{sender_name}: {message_text[:80]}")

            if message_text:
                self.add_to_history(sender_name, message_text, timestamp)

            if message_id in self.responded_messages:
                return

            if not self.is_bot_mentioned(data_message):
                return

            logger.info("Bot mentioned!")

            context = list(self.message_history)[:-1] if len(self.message_history) > 1 else []
            llm_response = self.get_llm_response(message_text, context)

            quote_author = envelope.get("sourceNumber") or envelope.get("source")
            try:
                self.client.send_message(
                    group_id,
                    llm_response,
                    quote_timestamp=timestamp,
                    quote_author=quote_author
                )
            except SignalError as e:
                logger.error(f"Failed to send reply: {e}")
                return

            self.add_to_history("Marvin", llm_response, int(time.time() * 1000))
            self.responded_messages.add(message_id)

            if len(self.responded_messages) > 1000:
                old_messages = sorted(self.responded_messages)[:500]
                self.responded_messages -= set(old_messages)

        except Exception:
            logger.exception("Error processing message:")

    def run(self):
        """Listen for messages forever, reconnecting whenever the daemon goes
        away (it restarts on every signal-cli image update)"""
        logger.info(f"Marvin listening in group {self.group_id} as {self.bot_number}, LLM: {self.config['llm_url']}")

        delay = RECONNECT_MIN
        while True:
            try:
                with self.client.subscribe() as sock:
                    delay = RECONNECT_MIN
                    self.listen(sock)
                logger.warning("Connection closed by daemon")
            except DaemonUnreachable as e:
                logger.warning(f"signal-cli unreachable: {e}")
            except OSError:
                logger.exception("Lost connection to signal-cli:")

            logger.info(f"Reconnecting in {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX)

    def listen(self, sock):
        """Handle incoming messages until the connection closes"""
        buffer = b''
        while chunk := sock.recv(65536):
            buffer += chunk
            *lines, buffer = buffer.split(b'\n')
            for line in lines:
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    logger.warning(f"Failed to parse JSON: {line[:100]}")
                    continue

                params = message.get("params") or {}
                if message.get("id") == "subscribe":
                    # In the daemon's default receive mode messages are pushed
                    # to every client anyway, so an error here is harmless.
                    logger.info(f"Subscription: {message.get('result', message.get('error'))}")
                elif isinstance(params, dict) and "envelope" in params:
                    self.process_message(params["envelope"])
