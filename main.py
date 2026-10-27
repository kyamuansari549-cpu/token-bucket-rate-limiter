"""Token Bucket Rate Limiter - FastAPI service.

Run it with:

    uvicorn main:app --host 0.0.0.0 --port 8000

Configuration is read once at startup from environment variables.
Invalid values abort startup with a clear error (no silent defaults).

    RATE_LIMIT_CAPACITY    bucket size / max burst        (default: 10)
    RATE_LIMIT_REFILL_RATE tokens added per second        (default: 1.0)
    RATE_LIMIT_BACKEND     "memory" (default) or "redis"
    REDIS_URL              used when backend is "redis"
                           (default: redis://localhost:6379/0)

How limiting is applied
-----------------------
``RateLimitMiddleware`` limits every request by client key:

- the ``X-API-Key`` header value, when the header is present, else
- the client IP address.

Paths in ``EXCLUDED_PATHS`` are never limited. ``/api/consume`` is excluded
from the middleware on purpose and enforces the limit itself, because it
accepts a custom ``cost`` - the middleware would charge 1 token and the
endpoint another ``cost`` tokens (double charging). ``/api/limit-status``
is excluded so that checking your status never burns tokens.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Mapping, Optional, Tuple

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from rate_limiter import (
    BucketStatus,
    InMemoryRateLimiter,
    RateLimiter,
    RateLimitResult,
)

#: Paths that are never rate-limited by the middleware.
EXCLUDED_PATHS = frozenset(
    {
        "/health",  # health checks must always pass
        "/docs",  # interactive API docs
        "/openapi.json",  # schema the docs page loads
        "/redoc",
        "/api/consume",  # enforces the limit itself (custom cost)
        "/api/limit-status",  # read-only; must not burn tokens
    }
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    capacity: int = 10
    refill_rate: float = 1.0
    backend: str = "memory"
    redis_url: str = "redis://localhost:6379/0"


def load_settings(env: Optional[Mapping[str, str]] = None) -> Settings:
    """Build :class:`Settings` from environment variables.

    Raises ``ValueError`` with a clear message on any invalid value.
    """
    env = os.environ if env is None else env

    raw_capacity = env.get("RATE_LIMIT_CAPACITY", "10")
    try:
        capacity = int(raw_capacity)
    except ValueError:
        raise ValueError(
            f"RATE_LIMIT_CAPACITY must be an integer, got {raw_capacity!r}"
        ) from None
    if capacity <= 0:
        raise ValueError(f"RATE_LIMIT_CAPACITY must be > 0, got {capacity}")

    raw_rate = env.get("RATE_LIMIT_REFILL_RATE", "1.0")
    try:
        refill_rate = float(raw_rate)
    except ValueError:
        raise ValueError(
            f"RATE_LIMIT_REFILL_RATE must be a number, got {raw_rate!r}"
        ) from None
    if refill_rate <= 0:
        raise ValueError(f"RATE_LIMIT_REFILL_RATE must be > 0, got {refill_rate}")

    backend = env.get("RATE_LIMIT_BACKEND", "memory").strip().lower()
    if backend not in ("memory", "redis"):
        raise ValueError(
            f"RATE_LIMIT_BACKEND must be 'memory' or 'redis', got {backend!r}"
        )

    return Settings(
        capacity=capacity,
        refill_rate=refill_rate,
        backend=backend,
        redis_url=env.get("REDIS_URL", "redis://localhost:6379/0"),
    )


def build_limiter(settings: Settings) -> RateLimiter:
    """Create the limiter backend selected in ``settings``."""
    if settings.backend == "redis":
        from rate_limiter import RedisRateLimiter

        return RedisRateLimiter(
            capacity=settings.capacity,
            refill_rate=settings.refill_rate,
            redis_url=settings.redis_url,
        )
    return InMemoryRateLimiter(
        capacity=settings.capacity, refill_rate=settings.refill_rate
    )


# ---------------------------------------------------------------------------
# Helpers shared by the middleware and the self-limiting endpoint
# ---------------------------------------------------------------------------


def client_key(request: Request) -> Tuple[str, str]:
    """Return ``(bucket_key, key_type)`` for the caller.

    An ``X-API-Key`` header wins over the client IP, so different API keys
    behind one IP (NAT, shared office) still get separate buckets.
    """
    api_key = request.headers.get("x-api-key")
    if api_key:
        return f"apikey:{api_key}", "api_key"
    host = request.client.host if request.client else "unknown"
    return f"ip:{host}", "ip"


def rate_limit_headers(result: RateLimitResult) -> dict:
    """Standard X-RateLimit-* headers for a check result."""
    return {
        "X-RateLimit-Limit": str(result.limit),
        # Whole tokens the client can still spend right now.
        "X-RateLimit-Remaining": str(max(0, int(result.remaining))),
        # Seconds until the bucket is full again.
        "X-RateLimit-Reset": str(int(math.ceil(result.reset_after_seconds))),
    }


def too_many_requests_response(result: RateLimitResult) -> JSONResponse:
    """Build the HTTP 429 response for a denied check."""
    headers = rate_limit_headers(result)
    if math.isfinite(result.retry_after_seconds):
        headers["Retry-After"] = str(max(1, math.ceil(result.retry_after_seconds)))
        retry_after = round(result.retry_after_seconds, 3)
    else:
        retry_after = None  # cost > capacity: waiting will never help
    body = {
        "error": "rate_limit_exceeded",
        "message": "Too many requests. Please slow down and retry later.",
        "retry_after_seconds": retry_after,
    }
    return JSONResponse(status_code=429, content=body, headers=headers)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Charge 1 token per request (except excluded paths); 429 when empty."""

    def __init__(self, app, limiter: RateLimiter) -> None:
        super().__init__(app)
        self.limiter = limiter

    async def dispatch(self, request: Request, call_next):
        if request.url.path in EXCLUDED_PATHS:
            return await call_next(request)

        key, _key_type = client_key(request)
        result = self.limiter.allow(key, cost=1)
        if not result.allowed:
            return too_many_requests_response(result)

        response = await call_next(request)
        for name, value in rate_limit_headers(result).items():
            response.headers[name] = value
        return response


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings if settings is not None else load_settings()
    limiter = build_limiter(settings)

    app = FastAPI(
        title="Token Bucket Rate Limiter",
        description="A small FastAPI service demonstrating per-client "
        "token-bucket rate limiting.",
        version="1.0.0",
    )
    app.add_middleware(RateLimitMiddleware, limiter=limiter)
    app.state.limiter = limiter
    app.state.settings = settings

    # -- endpoints ----------------------------------------------------------

    @app.get("/health")
    async def health():
        """Liveness probe. Never rate-limited."""
        return {"status": "ok"}

    @app.get("/api/data")
    async def get_data():
        """Demo resource, rate-limited by the middleware (cost 1)."""
        return {
            "data": "sample-payload",
            "message": "If you can read this, a token was spent for you.",
        }

    @app.post("/api/consume")
    async def consume(request: Request):
        """Spend ``cost`` tokens (default 1) from the caller's bucket.

        Body (optional JSON): ``{"cost": 3}``. Enforces the limit itself
        because the cost is variable; see the module docstring.
        """
        cost = 1.0
        if request.headers.get("content-type", "").startswith("application/json"):
            try:
                payload = await request.json()
            except Exception:
                return JSONResponse(
                    status_code=400,
                    content={"error": "invalid_json", "message": "Body must be valid JSON."},
                )
            if isinstance(payload, dict) and "cost" in payload:
                cost = payload["cost"]
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost <= 0:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_cost",
                    "message": f"cost must be a positive number, got {cost!r}",
                },
            )
        if cost > settings.capacity:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "cost_exceeds_capacity",
                    "message": f"cost {cost} can never fit in a bucket of "
                    f"capacity {settings.capacity}",
                },
            )

        key, key_type = client_key(request)
        result = limiter.allow(key, cost=float(cost))
        if not result.allowed:
            return too_many_requests_response(result)
        return JSONResponse(
            content={
                "consumed": cost,
                "key_type": key_type,
                "tokens_remaining": round(result.remaining, 3),
            },
            headers=rate_limit_headers(result),
        )

    @app.get("/api/limit-status")
    async def limit_status(request: Request):
        """The caller's current bucket state. Never consumes tokens."""
        key, key_type = client_key(request)
        status: BucketStatus = limiter.status(key)
        return {
            "key_type": key_type,
            "capacity": status.capacity,
            "refill_rate_per_second": status.refill_rate,
            "tokens_remaining": round(status.tokens, 3),
            "full_after_seconds": round(status.full_after_seconds, 3),
        }

    return app


#: The app uvicorn serves. Settings are loaded (and validated) at import.
app = create_app()
