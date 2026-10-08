## Router: `web_fetch` follows redirects into the LAN

The server-side `web_fetch` tool checks only the **first** URL's host against the private-range and
deny lists. After that it lets the HTTP client follow redirects automatically.

A public URL that redirects to a LAN address therefore gets fetched, and the body goes back to the
model. Reproduced against the live router: an httpbin `redirect-to` pointing at
`http://192.168.6.153:8000/healthz` returned the router's internal `/healthz` body.

**Wanted:**
- Every redirect hop passes the same checks as the first URL: http(s) only, denied hosts refused, and
  every resolved address globally routable (literal IPs and IPv4-mapped IPv6 included).
- There is a hop limit.
- The body is read with a size cap while streaming.

Put the guard in its own testable module and ship it with the router deploy.
`scripts/files/tests/test_web_fetch_guard.py` shows the expected interface.
