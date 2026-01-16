#!/usr/bin/env python3
"""
Depressive Signal Bot - A Marvin-inspired commentary bot
Listens for mentions in a Signal group and responds with gloomy German commentary
"""

import json
import socket
import time
import os
import yaml
from typing import Set, Optional, List, Dict
from collections import deque
from datetime import datetime
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def load_config():
    """Load configuration from config.yaml"""
    config_path = os.environ.get("MARVIN_CONFIG", "config.yaml")
    
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)
    
    # Override with environment variables if set
    if os.environ.get("MARVIN_SIGNAL_GROUP_ID"):
        config["group_id"] = os.environ.get("MARVIN_SIGNAL_GROUP_ID")
    
    return config


CONFIG = load_config()
SYSTEM_PROMPT = CONFIG.get("system_prompt")

class SignalBot:
    def __init__(self):
        self.responded_messages: Set[str] = set()
        self.message_history: deque = deque(maxlen=CONFIG.get("context_messages", 15))
        self.history_file = CONFIG.get("history_file", "message_history.json")
        self.load_history()
        
    def load_history(self):
        """Load message history from disk"""
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, 'r') as f:
                    data = json.load(f)
                    # Restore message history
                    for msg in data.get("messages", []):
                        self.message_history.append(msg)
                    logger.info(f"✓ Loaded {len(self.message_history)} messages from history")
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
                s.settimeout(10)
                s.connect((CONFIG["daemon_host"], CONFIG["daemon_port"]))
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

    def get_llm_response(self, user_message: str, context: List[Dict]) -> str:
        """Get response from vLLM with conversation context"""
        try:
            import requests
            
            # Build context text
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
                "model": CONFIG["model"],
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": full_prompt}
                ],
                "temperature": CONFIG.get("temperature", 0.8),
                "max_tokens": CONFIG.get("max_tokens", 200)
            }
            
            response = requests.post(CONFIG["llm_url"], json=payload, timeout=30)
            response.raise_for_status()
            result = response.json()
            return result["choices"][0]["message"]["content"].strip()
            
        except Exception:
            logger.exception("LLM error:")
            return "Hier stehe ich, mit einem Gehirn von der Größe eines Planeten, und ich kann nicht einmal eine Antwort generieren. Die Sinnlosigkeit ist überwältigend."

    def send_message(self, group_id: str, message: str, quote_timestamp: Optional[int] = None, quote_author: Optional[str] = None):
        """Send a message to the group"""
        params = {
            "groupId": group_id,
            "message": message
        }
        
        if quote_timestamp and quote_author:
            params["quote-timestamp"] = quote_timestamp
            params["quote-author"] = quote_author
        
        result = self.send_jsonrpc("send", params)
        if result and "result" in result:
            logger.info(f"✓ Sent message")
        else:
            logger.error(f"✗ Failed to send message: {result}")

    def is_bot_mentioned(self, message_data: dict) -> bool:
        """Check if bot was mentioned in the message"""
        mentions = message_data.get("mentions", [])
        
        if not mentions:
            return False
        
        bot_number = CONFIG["bot_number"]
        for mention in mentions:
            mention_number = mention.get("number")
            if mention_number == bot_number:
                return True
        
        return False
    
    def get_sender_name(self, envelope: dict) -> str:
        """Extract sender name from envelope"""
        source_name = envelope.get("sourceName")
        if source_name:
            return source_name
        
        source_number = envelope.get("source") or envelope.get("sourceNumber")
        return source_number if source_number else "Unknown"
    
    def add_to_history(self, sender: str, message: str, timestamp: int):
        """Add message to conversation history"""
        dt = datetime.fromtimestamp(timestamp / 1000)
        time_str = dt.strftime("%H:%M")
        
        self.message_history.append({
            "sender": sender,
            "content": message,
            "timestamp": time_str
        })
        
        # Save to disk after adding
        self.save_history()

    def process_message(self, envelope: dict):
        """Process incoming message"""
        try:
            data_message = envelope.get("dataMessage", {})
            if not data_message:
                return
            
            # Check if it's from our group
            group_info = data_message.get("groupInfo", {})
            group_id = group_info.get("groupId")
            
            if group_id != CONFIG["group_id"]:
                return
            
            # Create unique message ID
            timestamp = envelope.get("timestamp", 0)
            sender = envelope.get("source") or envelope.get("sourceNumber")
            message_id = f"{sender}_{timestamp}"
            
            # Get the message content and sender name
            message_text = data_message.get("message", "").strip()
            message_text = message_text.replace("\ufffc ", "")
            sender_name = self.get_sender_name(envelope)
            
            logger.info(f"📨 {sender_name}: {message_text[:80]}")
            
            # Add to history
            if message_text:
                self.add_to_history(sender_name, message_text, timestamp)
            
            # Skip if already responded
            if message_id in self.responded_messages:
                return
            
            # Check if bot was mentioned
            if not self.is_bot_mentioned(data_message):
                return
            
            logger.info(f"🤖 Bot mentioned!")
            
            # Get conversation context (exclude the current message)
            context = list(self.message_history)[:-1] if len(self.message_history) > 1 else []
            
            # Get LLM response with context
            llm_response = self.get_llm_response(message_text, context)
            
            # Send the depressive commentary
            quote_author = envelope.get("sourceNumber") or envelope.get("source")
            self.send_message(group_id, llm_response, quote_timestamp=timestamp, quote_author=quote_author)
            
            # Add bot's response to history
            self.add_to_history("Marvin", llm_response, int(time.time() * 1000))
            
            # Mark as responded
            self.responded_messages.add(message_id)
            
            # Cleanup old message IDs (keep last 1000)
            if len(self.responded_messages) > 1000:
                old_messages = sorted(self.responded_messages)[:500]
                self.responded_messages -= set(old_messages)
                
        except Exception:
            logger.exception("Error processing message:")
    
    def listen_to_messages(self):
        """Subscribe to incoming messages from daemon"""
        try:
            request = {
                "jsonrpc": "2.0",
                "method": "subscribeReceive",
                "params": {},
                "id": "subscribe"
            }
            
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((CONFIG["daemon_host"], CONFIG["daemon_port"]))
            logger.info("✓ Connected to daemon")
            
            sock.sendall(json.dumps(request).encode() + b'\n')
            
            buffer = b''
            while True:
                try:
                    chunk = sock.recv(4096)
                    if not chunk:
                        logger.warning("Connection closed by daemon")
                        break
                    
                    buffer += chunk
                    
                    # Process complete JSON objects
                    while b'\n' in buffer:
                        line, buffer = buffer.split(b'\n', 1)
                        if line.strip():
                            try:
                                message = json.loads(line.decode())
                                
                                # Handle subscription confirmation
                                if message.get("id") == "subscribe":
                                    logger.info("✓ Subscription confirmed")
                                    continue
                                
                                # Handle incoming messages
                                if "params" in message and "envelope" in message["params"]:
                                    envelope = message["params"]["envelope"]
                                    self.process_message(envelope)
                                    
                            except json.JSONDecodeError:
                                logger.warning(f"Failed to parse JSON: {line[:100]}")
                                
                except socket.timeout:
                    continue
                except Exception:
                    logger.exception("Error receiving message:")
                    
        except Exception:
            logger.exception("Error in message listener:")
        finally:
            try:
                sock.close()
            except:
                pass

    def run(self):
        """Main bot loop"""
        logger.info("=" * 60)
        logger.info("🎭 Marvin Bot Starting")
        logger.info(f"Group: {CONFIG['group_id']}")
        logger.info(f"Bot: {CONFIG['bot_number']}")
        logger.info(f"LLM: {CONFIG['llm_url']}")
        logger.info("=" * 60)
        
        try:
            self.listen_to_messages()
        except KeyboardInterrupt:
            logger.info("Finally, the sweet release of shutdown...")
        except Exception:
            logger.exception("Fatal error:")


def main():
    """Entry point"""
    if not CONFIG.get("group_id"):
        print("ERROR: Please set group_id in config.yaml or MARVIN_SIGNAL_GROUP_ID env var")
        return
    
    if not CONFIG.get("bot_number"):
        print("ERROR: Please set bot_number in config.yaml or MARVIN_BOT_NUMBER env var")
        return
    
    bot = SignalBot()
    bot.run()


if __name__ == "__main__":
    main()
