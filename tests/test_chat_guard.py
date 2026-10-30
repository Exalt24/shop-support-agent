"""The chat guards on their own: signed conversation ids, the rate limiter, the body cap, plain text, the client address.

The signer and the limiter remember things between calls, so besides examples they run over seeded random worlds against a
plain reference model (the same idea as test_leads_property.py)."""
import asyncio
import random

import pytest

from agent.chat_guard import (BodyTooLarge, ConversationSigner, RateLimiter, client_ip, plain_text, read_capped)

KEY = b"k" * 32


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


# ---- signed conversation ids ----------------------------------------------------------------------------------------------

def test_a_token_verifies_for_its_tenant_only():
    s = ConversationSigner(KEY)
    token = s.issue("harbour-heating")
    cid = s.verify(token, "harbour-heating")
    assert cid and cid in token
    assert s.verify(token, "loomhouse-homewares") is None


def test_a_forged_edited_or_unsigned_token_is_refused():
    s, other = ConversationSigner(KEY), ConversationSigner(b"x" * 32)
    token = s.issue("harbour-heating")
    tenant, cid, issued, sig = token.split(".")
    forged = ".".join([tenant, "AAAAAAAAAAAAAAAAAAAAAA", issued, sig])
    assert s.verify(forged, "harbour-heating") is None                       # another visitor's id with this signature
    assert s.verify(".".join([tenant, cid, issued]), "harbour-heating") is None  # no signature
    assert s.verify(".".join([tenant, cid, issued, "x" * len(sig)]), "harbour-heating") is None
    assert other.verify(token, "harbour-heating") is None                    # signed with a different key
    for bad in (None, 5, "", "a.b.c.d", token + "x", "." * 3, "x" * 500, ["a"]):
        assert s.verify(bad, "harbour-heating") is None


def test_a_token_expires():
    clock = Clock()
    s = ConversationSigner(KEY, now=clock, max_age=100)
    token = s.issue("t-one")
    clock.t += 99
    assert s.verify(token, "t-one")
    clock.t += 2
    assert s.verify(token, "t-one") is None


def test_the_signing_key_has_to_be_long_enough():
    with pytest.raises(ValueError):
        ConversationSigner(b"short")


def test_property_no_single_character_change_to_a_token_verifies():
    rng = random.Random(7)
    s = ConversationSigner(KEY)
    for _ in range(60):
        token = s.issue(rng.choice(["alpha-shop", "beta-shop"]))
        tenant = token.split(".")[0]
        assert s.verify(token, tenant)
        i = rng.randrange(len(token))
        swap = rng.choice([c for c in "abcdefghijklmnopqrstuvwxyz0123456789-_." if c != token[i]])
        assert s.verify(token[:i] + swap + token[i + 1:], tenant) is None, (token, i, swap)


def test_property_ids_are_unique_and_bound_to_their_tenant():
    s = ConversationSigner(KEY)
    seen = set()
    for i in range(1500):
        tenant = "tenant-%d" % (i % 3)
        token = s.issue(tenant)
        cid = s.verify(token, tenant)
        assert cid and cid not in seen
        seen.add(cid)
        assert s.verify(token, "tenant-%d" % ((i + 1) % 3)) is None


# ---- the rate limiter ---------------------------------------------------------------------------------------------------------

def test_the_limiter_allows_the_limit_then_says_how_long_to_wait():
    clock = Clock()
    rl = RateLimiter(3, 60, now=clock)
    assert [rl.hit("a")[0] for _ in range(3)] == [True, True, True]
    ok, wait = rl.hit("a")
    assert not ok and 1 <= wait <= 61
    assert rl.hit("b")[0]                      # another key has its own budget
    clock.t += 61
    assert rl.hit("a")[0]


def test_the_limiter_forgets_old_keys_instead_of_growing():
    rl = RateLimiter(1, 60, now=Clock(), max_keys=50)
    for i in range(500):
        rl.hit("k%d" % i)
    assert len(rl._hits) <= 50


def test_property_the_limiter_matches_a_plain_reference_over_random_worlds():
    for seed in range(40):
        rng = random.Random(seed)
        limit, window = rng.randint(1, 6), rng.choice([10.0, 60.0])
        clock = Clock(0.0)
        rl = RateLimiter(limit, window, now=clock, max_keys=10000)
        allowed: dict[str, list[float]] = {}
        for _ in range(300):
            clock.t += rng.choice([0, 0, 0.5, 3, 11, 70])
            key = rng.choice(["ip:1", "ip:2", "ip:3", "conv:a"])
            recent = [t for t in allowed.get(key, []) if clock.t - t < window]
            expected = len(recent) < limit
            ok, wait = rl.hit(key)
            assert ok == expected, (seed, key, clock.t)
            if ok:
                allowed.setdefault(key, []).append(clock.t)
            else:
                assert wait >= 1
        for key, times in allowed.items():  # never more than `limit` allowed inside any one window
            for t in times:
                assert len([x for x in times if t <= x < t + window]) <= limit


# ---- the body cap, plain text, the client address ------------------------------------------------------------------------

def chunks(*parts):
    async def gen():
        for p in parts:
            yield p
    return gen()


def test_a_streamed_body_is_cut_off_at_the_cap():
    assert asyncio.run(read_capped(chunks(b"ab", b"cd"), 4)) == b"abcd"
    with pytest.raises(BodyTooLarge):
        asyncio.run(read_capped(chunks(b"ab", b"cde"), 4))
    with pytest.raises(BodyTooLarge):  # one chunk far over the cap
        asyncio.run(read_capped(chunks(b"x" * 100), 4))


def test_plain_text_drops_control_characters_and_caps_the_length():
    assert plain_text("a\x00b\x1b[31mc\x7f d") == "ab[31mcd"
    assert plain_text("line one\nline two\ttabbed") == "line one\nline two\ttabbed"
    assert plain_text("<b>kept as written</b>") == "<b>kept as written</b>"
    assert len(plain_text("x" * 9000)) == 2000


def test_the_client_address_ignores_forwarding_headers_unless_told_to_trust_a_proxy():
    assert client_ip("10.0.0.5", "1.2.3.4", False) == "10.0.0.5"
    assert client_ip("10.0.0.5", "1.2.3.4, 9.9.9.9", True) == "9.9.9.9"   # the proxy's entry, not the one a visitor typed
    assert client_ip("10.0.0.5", None, True) == "10.0.0.5"
    assert client_ip(None, None, False) == "unknown"
