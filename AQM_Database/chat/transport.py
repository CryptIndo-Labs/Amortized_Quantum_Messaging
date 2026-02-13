"""
Redis pub/sub transport for terminal-to-terminal chat.

Uses a separate Redis connection with decode_responses=True (pub/sub carries
JSON strings, not binary blobs), independent from vault/inventory clients.
"""

import threading
from typing import Callable, Optional

import redis

from AQM_Database.aqm_shared import config
from AQM_Database.chat.protocol import channel_for, serialize, deserialize, ChatMessage


class ChatTransport:
    """Publish/subscribe wrapper over Redis pub/sub."""

    def __init__(self, redis_client: Optional[redis.Redis] = None):
        if redis_client is not None:
            self._redis = redis_client
        else:
            self._redis = redis.Redis(
                host=config.REDIS_HOST,
                port=config.REDIS_PORT,
                db=config.REDIS_VAULT_DB,
                decode_responses=True,
                socket_connect_timeout=config.REDIS_SOCKET_TIMEOUT,
            )
        self._pubsub: Optional[redis.client.PubSub] = None
        self._thread: Optional[threading.Thread] = None

    def publish(self, recipient_id: str, msg: ChatMessage) -> int:
        """Publish a ChatMessage to the recipient's channel.

        Returns the number of subscribers that received the message.
        """
        channel = channel_for(recipient_id)
        return self._redis.publish(channel, serialize(msg))

    def subscribe(self, user_id: str, callback: Callable[[ChatMessage], None]) -> None:
        """Subscribe to messages on the user's channel.

        Runs the listener in a daemon thread. The callback is called for each
        incoming ChatMessage.
        """
        channel = channel_for(user_id)
        self._pubsub = self._redis.pubsub()
        self._pubsub.subscribe(channel)

        def _listen():
            for raw in self._pubsub.listen():
                if raw["type"] != "message":
                    continue
                try:
                    msg = deserialize(raw["data"])
                    callback(msg)
                except Exception:
                    pass

        self._thread = threading.Thread(target=_listen, daemon=True)
        self._thread.start()

    def unsubscribe(self) -> None:
        """Unsubscribe and clean up."""
        if self._pubsub is not None:
            self._pubsub.unsubscribe()
            self._pubsub.close()
            self._pubsub = None

    def close(self) -> None:
        """Close transport and underlying Redis connection."""
        self.unsubscribe()
        self._redis.close()
