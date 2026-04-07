"""
RabbitMQ messaging client wrapper with connection retry, 
publish/subscribe helpers, and dead-letter queue configuration.
"""

import os
import json
import asyncio
import logging
from typing import Callable, Optional
from datetime import datetime

import aio_pika
from aio_pika import Message, DeliveryMode, ExchangeType

from shared.tracing import get_correlation_id, set_correlation_id

logger = logging.getLogger(__name__)

RABBITMQ_URL = os.getenv(
    "RABBITMQ_URL", "amqp://journey_admin:journey_pass@rabbitmq:5672/journey_vhost"
)

# Exchange and queue names
EVENTS_EXCHANGE = "journey_events"
DLX_EXCHANGE = "journey_events_dlx"

# Queue names per service
NOTIFICATION_QUEUE = "notification_events"
ENFORCEMENT_QUEUE = "enforcement_events"
ANALYTICS_QUEUE = "analytics_events"
CONFLICT_RESPONSE_QUEUE = "conflict_response_events"


class MessageBroker:
    """Async RabbitMQ client with automatic reconnection."""

    def __init__(self, url: str = None):
        self.url = url or RABBITMQ_URL
        self._connection: Optional[aio_pika.RobustConnection] = None
        self._channel: Optional[aio_pika.Channel] = None
        self._exchange: Optional[aio_pika.Exchange] = None

    async def connect(self, max_retries: int = 10, retry_delay: float = 3.0):
        """Establish connection to RabbitMQ with retry logic."""
        for attempt in range(1, max_retries + 1):
            try:
                self._connection = await aio_pika.connect_robust(self.url)
                self._channel = await self._connection.channel()
                await self._channel.set_qos(prefetch_count=10)

                # Declare the dead-letter exchange
                dlx_exchange = await self._channel.declare_exchange(
                    DLX_EXCHANGE, ExchangeType.FANOUT, durable=True
                )

                # Declare dead-letter queue
                dlq = await self._channel.declare_queue(
                    "dead_letter_queue", durable=True
                )
                await dlq.bind(dlx_exchange)

                # Declare the main events exchange
                self._exchange = await self._channel.declare_exchange(
                    EVENTS_EXCHANGE, ExchangeType.TOPIC, durable=True
                )

                logger.info("Connected to RabbitMQ successfully")
                return
            except Exception as e:
                logger.warning(
                    f"RabbitMQ connection attempt {attempt}/{max_retries} failed: {e}"
                )
                if attempt < max_retries:
                    await asyncio.sleep(retry_delay)
                else:
                    logger.error("Failed to connect to RabbitMQ after all retries")
                    raise

    async def close(self):
        """Close the RabbitMQ connection."""
        if self._connection and not self._connection.is_closed:
            await self._connection.close()
            logger.info("RabbitMQ connection closed")

    async def publish(self, routing_key: str, data: dict):
        """Publish a message to the events exchange."""
        if not self._exchange:
            raise RuntimeError("Not connected to RabbitMQ. Call connect() first.")

        # Serialize datetime objects
        def json_serializer(obj):
            if isinstance(obj, datetime):
                return obj.isoformat()
            raise TypeError(f"Type {type(obj)} not serializable")

        message = Message(
            body=json.dumps(data, default=json_serializer).encode(),
            delivery_mode=DeliveryMode.PERSISTENT,
            content_type="application/json",
            correlation_id=get_correlation_id(),
        )

        await self._exchange.publish(message, routing_key=routing_key)
        logger.debug(f"Published message with routing_key={routing_key}")

    async def subscribe(
        self,
        queue_name: str,
        routing_keys: list[str],
        callback: Callable,
    ):
        """
        Subscribe to messages matching the given routing keys.
        The callback receives the parsed JSON data as a dict.
        """
        if not self._channel:
            raise RuntimeError("Not connected to RabbitMQ. Call connect() first.")

        # Declare queue with dead-letter exchange
        queue = await self._channel.declare_queue(
            queue_name,
            durable=True,
            arguments={
                "x-dead-letter-exchange": DLX_EXCHANGE,
                "x-message-ttl": 86400000,  # 24h TTL
            },
        )

        # Bind queue to each routing key
        for key in routing_keys:
            await queue.bind(self._exchange, routing_key=key)
            logger.info(f"Queue '{queue_name}' bound to routing_key '{key}'")

        async def _process_message(message: aio_pika.IncomingMessage):
            async with message.process():
                try:
                    # Restore correlation ID context for this event consumer
                    if message.correlation_id:
                        set_correlation_id(message.correlation_id)
                        
                    data = json.loads(message.body.decode())
                    await callback(data, message.routing_key)
                except Exception as e:
                    logger.error(
                        f"Error processing message on {queue_name}: {e}",
                        exc_info=True,
                    )
                    # Message will be nacked and sent to DLQ
                    raise

        await queue.consume(_process_message)
        logger.info(f"Consuming from queue '{queue_name}'")

    @property
    def is_connected(self) -> bool:
        return (
            self._connection is not None
            and not self._connection.is_closed
        )


# Singleton instance
_broker: Optional[MessageBroker] = None


async def get_broker() -> MessageBroker:
    """Get or create the global MessageBroker instance."""
    global _broker
    if _broker is None or not _broker.is_connected:
        _broker = MessageBroker()
        await _broker.connect()
    return _broker


async def close_broker():
    """Close the global MessageBroker instance."""
    global _broker
    if _broker:
        await _broker.close()
        _broker = None


async def get_dlq_messages(limit: int = 10) -> list[dict]:
    """Peek at messages currently in the dead-letter queue."""
    broker = await get_broker()
    if not broker._channel:
        raise RuntimeError("Not connected to RabbitMQ.")
    
    queue = await broker._channel.declare_queue("dead_letter_queue", durable=True)
    messages = []
    for _ in range(limit):
        try:
            msg = await queue.get(timeout=0.1, no_ack=False)
            messages.append({
                "message_id": msg.message_id,
                "routing_key": msg.routing_key,
                "correlation_id": msg.correlation_id,
                "body": msg.body.decode() if msg.body else None,
            })
            await msg.reject(requeue=True)
        except aio_pika.exceptions.QueueEmpty:
            break
    return messages


async def replay_dlq() -> int:
    """Replay all messages from the DLQ back to the main exchange."""
    broker = await get_broker()
    if not broker._channel or not broker._exchange:
         raise RuntimeError("Not connected to RabbitMQ.")
         
    queue = await broker._channel.declare_queue("dead_letter_queue", durable=True)
    replayed = 0
    while True:
        try:
            msg = await queue.get(timeout=0.1, no_ack=False)
            # Keep original properties but strip DLQ specific headers if needed
            new_msg = Message(
                body=msg.body,
                delivery_mode=msg.delivery_mode,
                content_type=msg.content_type,
                correlation_id=msg.correlation_id,
            )
            await broker._exchange.publish(new_msg, routing_key=msg.routing_key)
            await msg.ack()
            replayed += 1
            logger.info(f"Replayed message with routing_key {msg.routing_key}")
        except aio_pika.exceptions.QueueEmpty:
            break
    return replayed
