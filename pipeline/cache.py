"""Cache key shared by the API (lookup) and the worker (write)."""
import hashlib


def cache_key(payload: dict) -> str:
    """Deterministic cache key scoped to the ticket.

    Python's hash() is salted per process (PYTHONHASHSEED), so the API and
    the worker never agreed on a key; and a key built from the text alone
    returned one reporter's ticket to another reporter with the same text.
    """
    material = "|".join([payload["ticket_id"], payload["reporter_email"], payload["raw_text"]])
    return "cache:" + hashlib.sha256(material.encode("utf-8")).hexdigest()
