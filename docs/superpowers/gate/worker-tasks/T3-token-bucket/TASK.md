# T3 — Token-bucket rate limiter

Implement `TokenBucket` in `token_bucket.py`. Every rule below is tested; nothing else is.

- `TokenBucket(rate, capacity, clock=time.monotonic)`.
  - `rate` is tokens per second, a float. `rate <= 0` raises `ValueError`.
  - `capacity` is an int. `capacity < 1` raises `ValueError`.
  - The bucket **starts full** (`capacity` tokens).
- **Refill.** Before any read or consume, add `elapsed_seconds * rate` tokens, capped at
  `capacity`, where `elapsed_seconds` is the time since the previous refill. Tokens are fractional
  (float); never round them.
- `allow(n=1)`:
  - If at least `n` tokens are available (after refill), consume `n` and return `True`.
  - Otherwise consume **nothing** and return `False`.
  - `n <= 0` raises `ValueError`. `n > capacity` raises `ValueError`, because it could never
    succeed.
- `tokens` (a read-only property) returns the current token count after refill, as a float.
- `wait_time(n=1)` returns the seconds until `allow(n)` would succeed: `0.0` if it would succeed
  now, otherwise `(n - tokens) / rate`. It consumes nothing. The same argument checks as `allow`
  apply.
