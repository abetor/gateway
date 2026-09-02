"""Versioned generic argv event transport."""

from .dispatch import (
    EVENT_SCHEMA_VERSION,
    RECEIPT_SCHEMA_VERSION,
    EventError,
    dispatch_event,
    read_receipt,
    telegram_event,
)

__all__ = [
    "EVENT_SCHEMA_VERSION",
    "RECEIPT_SCHEMA_VERSION",
    "EventError",
    "dispatch_event",
    "read_receipt",
    "telegram_event",
]
