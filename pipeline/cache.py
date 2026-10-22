"""Cache key shared by the API (lookup) and the worker (write)."""
import hashlib
import json


def cache_key(payload: dict) -> str:
    """Deterministic cache key scoped to the ticket.

    Python's hash() is salted per process (PYTHONHASHSEED), so the API and
    the worker never agreed on a key; and a key built from the text alone
    returned one reporter's ticket to another reporter with the same text.

    The fields are serialised as a JSON array rather than joined with a
    delimiter: with "|" as a separator, ticket_id "T|a@x" + reporter "b@x"
    collided with ticket_id "T" + reporter "a@x|b@x" (review on PR #9).
    """
    material = json.dumps([payload["ticket_id"], payload["reporter_email"], payload["raw_text"]],
                          separators=(",", ":"), ensure_ascii=False)
    return "cache:" + hashlib.sha256(material.encode("utf-8")).hexdigest()
