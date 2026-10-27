# Token Bucket Rate Limiter

A small, production-style rate limiting service built with FastAPI. It limits
how often each client (API key or IP address) can call an API, using the
token bucket algorithm - the same algorithm used by API gateways and major
platforms to protect backends and enforce fair usage.

## How the token bucket algorithm works

Every client owns a bucket of tokens:

- The bucket holds at most `capacity` tokens and starts full.
- Tokens drip into the bucket continuously at `refill_rate` tokens/second,
  never exceeding `capacity`.
- Each request costs tokens (usually 1). If the bucket holds enough, the
  cost is deducted and the request proceeds. If not, the request is rejected
  with HTTP 429 and the exact wait time is reported.

Two properties make this algorithm popular:

- **Bursts are allowed** up to `capacity`, so legitimate spiky traffic is fine.
- **The sustained rate is capped** at `refill_rate` requests/second on average.

### Worked example

Configuration: `capacity = 5`, `refill_rate = 1` token/second.

| Time | Event | Tokens after |
|------|-------|--------------|
| t = 0.0 s | bucket starts full | 5 |
| t = 0.0 s | 5 requests arrive at once - all allowed | 0 |
| t = 0.0 s | 6th request - **denied**, `retry_after = 1.0 s` | 0 |
| t = 1.0 s | 1 token has refilled, request allowed | 0 |
| t = 3.5 s | idle for 2.5 s: 2.5 tokens refilled | 2.5 |
| t = 103.5 s | long idle: refill is capped | 5 (not 102.5) |

Refilling is computed lazily on each access from elapsed time
(`time.monotonic`), so no background threads are needed and tokens can be
fractional.

## Architecture

```
rate_limiter.py   Pure algorithm. No web framework code.
                  - TokenBucket: one thread-safe bucket (injectable clock)
                  - InMemoryRateLimiter: one bucket per key, in a dict
                  - RedisRateLimiter (optional): same interface, buckets in
                    Redis, updated atomically by a Lua script so multiple
                    server processes share one limit
main.py           FastAPI app.
                  - RateLimitMiddleware charges 1 token per request,
                    keyed by X-API-Key header if present, else client IP
                  - Settings are read once from the environment at startup;
                    invalid values abort startup with a clear error
tests/            pytest suite: core algorithm (with a fake clock, fully
                  deterministic) plus HTTP-level tests via TestClient
```

Design notes:

- Per-key buckets are independent: one abusive client cannot exhaust
  another client's allowance.
- `/health` and the docs paths are never limited, so load-balancer health
  checks cannot be throttled.
- `/api/consume` enforces the limit itself because it supports a variable
  `cost`; the middleware skips it to avoid double charging.
- `/api/limit-status` is read-only and never consumes tokens.
- Denied requests consume nothing.

## Endpoints

| Method | Path | Limited | Description |
|--------|------|---------|-------------|
| GET | `/health` | No | Liveness probe, returns `{"status": "ok"}` |
| GET | `/api/data` | Yes, cost 1 | Demo resource |
| POST | `/api/consume` | Yes, cost = body `cost` (default 1) | Spend a variable number of tokens |
| GET | `/api/limit-status` | No | Caller's bucket state: tokens remaining, capacity, refill rate |

### Rate limit headers

Every limited response (success and denial) carries:

| Header | Meaning |
|--------|---------|
| `X-RateLimit-Limit` | Bucket capacity |
| `X-RateLimit-Remaining` | Whole tokens the client can still spend now |
| `X-RateLimit-Reset` | Seconds until the bucket is full again |

A denied request returns HTTP 429, a `Retry-After` header (whole seconds),
and a JSON body:

```json
{
  "error": "rate_limit_exceeded",
  "message": "Too many requests. Please slow down and retry later.",
  "retry_after_seconds": 9.932
}
```

## Running the service

### Linux / macOS

```bash
cd token-bucket-rate-limiter
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

### Windows (PowerShell)

```powershell
cd token-bucket-rate-limiter
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Then open http://127.0.0.1:8000/docs for the interactive API documentation.

## Demo

With the server running (default limits: capacity 10, refill 1/second), fire
12 requests at the demo endpoint:

```bash
for i in $(seq 1 12); do
  curl -s -o /dev/null -w "request $i -> %{http_code}\n" http://127.0.0.1:8000/api/data
done
```

Expected output - the first 10 requests succeed, then the bucket is empty:

```
request 1 -> 200
...
request 10 -> 200
request 11 -> 429
request 12 -> 429
```

Inspect a denial in full, check your bucket status, spend several tokens at
once, and use separate API keys:

```bash
curl -i http://127.0.0.1:8000/api/data            # see Retry-After + X-RateLimit-* headers on a 429
curl http://127.0.0.1:8000/api/limit-status        # your bucket state (free to call)
curl -X POST http://127.0.0.1:8000/api/consume -H "Content-Type: application/json" -d '{"cost": 3}'
curl http://127.0.0.1:8000/api/data -H "X-API-Key: my-secret-key"   # separate bucket per API key
```

## Configuration

| Environment variable | Default | Meaning |
|----------------------|---------|---------|
| `RATE_LIMIT_CAPACITY` | `10` | Bucket size = maximum burst per client |
| `RATE_LIMIT_REFILL_RATE` | `1.0` | Tokens added per second = sustained rate |
| `RATE_LIMIT_BACKEND` | `memory` | `memory` or `redis` |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection, used with the `redis` backend |

Example - allow a burst of 100 requests, sustained 20/second:

Linux / macOS:

```bash
RATE_LIMIT_CAPACITY=100 RATE_LIMIT_REFILL_RATE=20 uvicorn main:app --port 8000
```

Windows (PowerShell):

```powershell
$env:RATE_LIMIT_CAPACITY = "100"
$env:RATE_LIMIT_REFILL_RATE = "20"
uvicorn main:app --port 8000
```

Invalid values (for example `RATE_LIMIT_CAPACITY=abc`) stop startup with a
clear error such as `ValueError: RATE_LIMIT_CAPACITY must be an integer, got 'abc'`.

### Redis backend (optional)

The in-memory backend keeps buckets per server process. To share one limit
across several processes or machines, install the optional dependency, run a
Redis server, and set:

```bash
pip install redis
RATE_LIMIT_BACKEND=redis REDIS_URL=redis://localhost:6379/0 uvicorn main:app --port 8000
```

The Redis backend implements the same interface and performs the
refill-and-consume step atomically in a Lua script.

## Running the tests

```bash
pytest -q
```

The suite covers the burst limit, refill behaviour over time (deterministic,
via an injectable fake clock - no real sleeping), exact `retry_after` math,
per-key isolation, variable cost, capacity capping after long idle,
thread-safety under concurrent access, configuration validation, and the
HTTP behaviour of the middleware (200s, then 429 with `Retry-After` and
`X-RateLimit-*` headers; `/health` never limited).

## Project layout

```
token-bucket-rate-limiter/
|-- rate_limiter.py            # token bucket core + in-memory / Redis backends
|-- main.py                    # FastAPI app, middleware, endpoints, settings
|-- tests/
|   `-- test_rate_limiter.py   # pytest suite (24 tests)
|-- requirements.txt
|-- README.md
`-- LEARN.md                   # step-by-step walkthrough of how it all works
```
