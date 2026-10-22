"""Regression tests for the cache key (PR #9 review: delimiter collision)."""
from pipeline.cache import cache_key

FIRST = {"ticket_id": "T", "reporter_email": "alice@enterprise.com", "raw_text": "bob@enterprise.com|secret"}
SECOND = {"ticket_id": "T|alice@enterprise.com", "reporter_email": "bob@enterprise.com", "raw_text": "secret"}


def test_fields_cannot_be_shifted_across_the_separator():
    assert cache_key(FIRST) != cache_key(SECOND)


def test_key_is_stable_and_scoped():
    assert cache_key(FIRST) == cache_key(dict(FIRST))
    assert cache_key(FIRST) != cache_key({**FIRST, "ticket_id": "T2"})
    assert cache_key(FIRST) != cache_key({**FIRST, "reporter_email": "carol@enterprise.com"})
