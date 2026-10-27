"""Tests for the token bucket core and the FastAPI service.

Core tests use a FakeClock, so refill behaviour is deterministic and no
test ever sleeps.
"""

import threading

import pytest
from fastapi.testclient import TestClient

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import Settings, create_app, load_settings  # noqa: E402
from rate_limiter import InMemoryRateLimiter, TokenBucket  # noqa: E402


class FakeClock:
    """A monotonic clock the test controls by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# Core: TokenBucket
# ---------------------------------------------------------------------------


def make_bucket(capacity=5, rate=1.0):
    clock = FakeClock()
    return TokenBucket(capacity=capacity, refill_rate=rate, clock=clock), clock


def test_initial_burst_up_to_capacity_is_allowed():
    bucket, _ = make_bucket(capacity=5)
    results = [bucket.allow() for _ in range(5)]
    assert all(r.allowed for r in results)
    assert results[-1].remaining == 0


def test_capacity_plus_one_th_request_is_denied():
    bucket, _ = make_bucket(capacity=5)
    for _ in range(5):
        assert bucket.allow().allowed
    denied = bucket.allow()
    assert not denied.allowed
    assert denied.remaining == 0
    assert denied.retry_after_seconds > 0


def test_denied_request_consumes_nothing():
    bucket, _ = make_bucket(capacity=2)
    bucket.allow()
    bucket.allow()
    assert not bucket.allow().allowed
    status = bucket.status()
    assert status.tokens == 0  # the denial did not push it negative


def test_refill_over_time_allows_again():
    bucket, clock = make_bucket(capacity=3, rate=1.0)
    for _ in range(3):
        bucket.allow()
    assert not bucket.allow().allowed
    clock.advance(1.0)  # exactly 1 token refills
    assert bucket.allow().allowed
    assert not bucket.allow().allowed
    clock.advance(2.0)
    assert bucket.allow().allowed
    assert bucket.allow().allowed


def test_fractional_refill():
    bucket, clock = make_bucket(capacity=10, rate=2.0)  # 2 tokens / second
    for _ in range(10):
        bucket.allow()
    clock.advance(0.25)  # 0.5 tokens: not enough for cost 1
    assert not bucket.allow().allowed
    clock.advance(0.25)  # 1.0 tokens total
    assert bucket.allow().allowed


def test_retry_after_math_for_known_deficit():
    bucket, _ = make_bucket(capacity=10, rate=2.0)
    for _ in range(10):
        bucket.allow()
    result = bucket.allow(cost=3)  # deficit 3 tokens at 2 tokens/sec
    assert not result.allowed
    assert result.retry_after_seconds == pytest.approx(1.5)


def test_retry_after_partial_tokens():
    bucket, clock = make_bucket(capacity=10, rate=2.0)
    for _ in range(9):
        bucket.allow()  # 1 token left
    result = bucket.allow(cost=3)  # deficit 2 tokens at 2 tokens/sec
    assert not result.allowed
    assert result.retry_after_seconds == pytest.approx(1.0)
    clock.advance(1.0)
    assert bucket.allow(cost=3).allowed


def test_bucket_never_exceeds_capacity_after_long_idle():
    bucket, clock = make_bucket(capacity=4, rate=1.0)
    bucket.allow()
    bucket.allow()
    clock.advance(10_000)  # idle "forever"
    status = bucket.status()
    assert status.tokens == 4  # capped at capacity, not 2 + 10000
    for _ in range(4):
        assert bucket.allow().allowed
    assert not bucket.allow().allowed


def test_cost_greater_than_one():
    bucket, _ = make_bucket(capacity=5, rate=1.0)
    result = bucket.allow(cost=3)
    assert result.allowed
    assert result.remaining == 2
    assert not bucket.allow(cost=3).allowed
    assert bucket.allow(cost=2).allowed


def test_cost_above_capacity_can_never_be_allowed():
    bucket, clock = make_bucket(capacity=5, rate=1.0)
    result = bucket.allow(cost=6)
    assert not result.allowed
    assert result.retry_after_seconds == float("inf")
    clock.advance(100)
    assert not bucket.allow(cost=6).allowed


def test_invalid_arguments_raise():
    with pytest.raises(ValueError):
        TokenBucket(capacity=0, refill_rate=1.0)
    with pytest.raises(ValueError):
        TokenBucket(capacity=5, refill_rate=0)
    bucket, _ = make_bucket()
    with pytest.raises(ValueError):
        bucket.allow(cost=0)


def test_result_reports_limit_and_reset():
    bucket, _ = make_bucket(capacity=10, rate=2.0)
    result = bucket.allow(cost=4)  # 6 left; full again in (10 - 6) / 2 = 2s
    assert result.limit == 10
    assert result.remaining == 6
    assert result.retry_after_seconds == 0.0
    assert result.reset_after_seconds == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Core: per-key limiter
# ---------------------------------------------------------------------------


def test_per_key_isolation():
    clock = FakeClock()
    limiter = InMemoryRateLimiter(capacity=2, refill_rate=1.0, clock=clock)
    assert limiter.allow("user-a").allowed
    assert limiter.allow("user-a").allowed
    assert not limiter.allow("user-a").allowed
    # A different key has its own untouched bucket.
    assert limiter.allow("user-b").allowed
    assert limiter.allow("user-b").allowed
    assert not limiter.allow("user-b").allowed


def test_status_does_not_consume():
    clock = FakeClock()
    limiter = InMemoryRateLimiter(capacity=3, refill_rate=1.0, clock=clock)
    limiter.allow("k")
    assert limiter.status("k").tokens == 2
    assert limiter.status("k").tokens == 2  # still 2 after peeking again


def test_concurrent_access_never_over_issues():
    """20 threads race on one bucket; exactly `capacity` may pass."""
    capacity = 50
    clock = FakeClock()  # frozen clock: zero refill during the race
    bucket = TokenBucket(capacity=capacity, refill_rate=1.0, clock=clock)

    attempts_per_thread = 10
    thread_count = 20
    barrier = threading.Barrier(thread_count)
    allowed_counts = []
    results_lock = threading.Lock()

    def worker():
        local_allowed = 0
        barrier.wait()  # release all threads at the same instant
        for _ in range(attempts_per_thread):
            if bucket.allow().allowed:
                local_allowed += 1
        with results_lock:
            allowed_counts.append(local_allowed)

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(allowed_counts) == capacity
    assert bucket.status().tokens == 0


# ---------------------------------------------------------------------------
# Settings validation
# ---------------------------------------------------------------------------


def test_load_settings_defaults():
    settings = load_settings({})
    assert settings.capacity == 10
    assert settings.refill_rate == 1.0
    assert settings.backend == "memory"


def test_load_settings_rejects_bad_values():
    with pytest.raises(ValueError, match="RATE_LIMIT_CAPACITY"):
        load_settings({"RATE_LIMIT_CAPACITY": "abc"})
    with pytest.raises(ValueError, match="RATE_LIMIT_CAPACITY"):
        load_settings({"RATE_LIMIT_CAPACITY": "0"})
    with pytest.raises(ValueError, match="RATE_LIMIT_REFILL_RATE"):
        load_settings({"RATE_LIMIT_REFILL_RATE": "-1"})
    with pytest.raises(ValueError, match="RATE_LIMIT_BACKEND"):
        load_settings({"RATE_LIMIT_BACKEND": "bogus"})


# ---------------------------------------------------------------------------
# HTTP level: middleware + endpoints (FastAPI TestClient)
# ---------------------------------------------------------------------------


def make_client(capacity=3, refill_rate=0.01):
    # Very slow refill: no meaningful tokens return during a test run.
    app = create_app(Settings(capacity=capacity, refill_rate=refill_rate))
    return TestClient(app)


def test_data_endpoint_200s_then_429_with_headers():
    client = make_client(capacity=3)
    responses = [client.get("/api/data") for _ in range(5)]
    assert [r.status_code for r in responses] == [200, 200, 200, 429, 429]

    denied = responses[3]
    assert denied.json()["error"] == "rate_limit_exceeded"
    assert denied.json()["retry_after_seconds"] > 0
    assert int(denied.headers["Retry-After"]) >= 1
    assert denied.headers["X-RateLimit-Limit"] == "3"
    assert denied.headers["X-RateLimit-Remaining"] == "0"
    assert "X-RateLimit-Reset" in denied.headers


def test_successful_responses_carry_rate_limit_headers():
    client = make_client(capacity=3)
    first = client.get("/api/data")
    assert first.headers["X-RateLimit-Limit"] == "3"
    assert first.headers["X-RateLimit-Remaining"] == "2"
    second = client.get("/api/data")
    assert second.headers["X-RateLimit-Remaining"] == "1"


def test_health_is_never_rate_limited():
    client = make_client(capacity=2)
    for _ in range(15):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


def test_api_keys_have_separate_buckets():
    client = make_client(capacity=2)
    headers_a = {"X-API-Key": "key-a"}
    headers_b = {"X-API-Key": "key-b"}
    assert client.get("/api/data", headers=headers_a).status_code == 200
    assert client.get("/api/data", headers=headers_a).status_code == 200
    assert client.get("/api/data", headers=headers_a).status_code == 429
    # key-b and the bare IP are unaffected by key-a's exhaustion.
    assert client.get("/api/data", headers=headers_b).status_code == 200
    assert client.get("/api/data").status_code == 200


def test_consume_with_cost():
    client = make_client(capacity=5)
    response = client.post("/api/consume", json={"cost": 3})
    assert response.status_code == 200
    assert response.json()["consumed"] == 3
    assert response.json()["tokens_remaining"] == 2
    # Only 2 tokens left: another cost-3 spend is denied...
    assert client.post("/api/consume", json={"cost": 3}).status_code == 429
    # ...but a cost-2 spend still fits, and is charged exactly once.
    assert client.post("/api/consume", json={"cost": 2}).status_code == 200
    status = client.get("/api/limit-status")
    assert status.json()["tokens_remaining"] == pytest.approx(0, abs=0.05)


def test_consume_rejects_bad_cost():
    client = make_client(capacity=5)
    assert client.post("/api/consume", json={"cost": 0}).status_code == 400
    assert client.post("/api/consume", json={"cost": 99}).status_code == 400
    assert client.post("/api/consume", json={"cost": "a lot"}).status_code == 400
    # None of the rejected calls above consumed tokens.
    assert client.get("/api/limit-status").json()["tokens_remaining"] == 5


def test_limit_status_reflects_bucket_without_consuming():
    client = make_client(capacity=4)
    client.get("/api/data")  # spends 1 via middleware
    body = client.get("/api/limit-status").json()
    assert body["capacity"] == 4
    assert body["tokens_remaining"] == pytest.approx(3, abs=0.05)
    again = client.get("/api/limit-status").json()
    assert again["tokens_remaining"] == pytest.approx(3, abs=0.05)
