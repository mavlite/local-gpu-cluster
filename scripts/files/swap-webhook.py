#!/usr/bin/env python3
"""
swap-webhook.py — HTTP server that triggers model profile swaps on the Proxmox host.

Accepts POST /swap?profile=<name> with Bearer auth, runs swap-chat-model.sh
synchronously (blocks until the model is active), returns 200/500.

Deploy via scripts/52-swap-webhook.sh (installs as a systemd service on the host).
Config from /etc/swap-webhook.env (loaded by systemd EnvironmentFile):
  SWAP_WEBHOOK_KEY      — required; shared secret for Bearer auth
  SWAP_SCRIPT           — path to swap-chat-model.sh
                          (default: /root/local-gpu-cluster/scripts/swap-chat-model.sh)
  SWAP_WEBHOOK_PORT     — TCP port (default: 9100)
  SWAP_WEBHOOK_BIND     — bind address; 52-swap-webhook.sh sets this to the vmbr0 IP
                          so the endpoint is reachable from LXCs but not the internet
  SWAP_SUBPROC_TIMEOUT  — subprocess timeout in seconds (default: 1800, matches
                          swap-chat-model.sh's 30-minute warm-load deadline)
"""

import hmac
import json
import logging
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# ---------- Config (from EnvironmentFile=/etc/swap-webhook.env) ----------

SWAP_WEBHOOK_KEY = os.environ.get("SWAP_WEBHOOK_KEY", "")
SWAP_SCRIPT = os.environ.get(
    "SWAP_SCRIPT",
    "/root/local-gpu-cluster/scripts/swap-chat-model.sh",
)
SWAP_WEBHOOK_PORT = int(os.environ.get("SWAP_WEBHOOK_PORT", "9100"))
SWAP_WEBHOOK_BIND = os.environ.get("SWAP_WEBHOOK_BIND", "0.0.0.0")
SWAP_SUBPROC_TIMEOUT = int(os.environ.get("SWAP_SUBPROC_TIMEOUT", "1800"))

VALID_PROFILES = {"qwen3.6", "qwen3.6-fast", "coder", "devstral"}

# ---------- Logging (systemd captures stdout) ----------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("swap-webhook")

# ---------- Single-flight lock (one swap at a time) ----------

# The router's asyncio _swap_lock already serializes router-side requests, so
# this threading.Lock is a belt-and-suspenders guard for direct/manual calls.
_swap_lock = threading.Lock()


# ---------- Request handler ----------

class SwapHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def _send_json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass

    def _authorized(self) -> bool:
        if not SWAP_WEBHOOK_KEY:
            return False
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        return hmac.compare_digest(auth[7:].strip(), SWAP_WEBHOOK_KEY)

    def do_GET(self):
        if urlparse(self.path).path == "/healthz":
            self._send_json(200, {"ok": True, "lock_held": _swap_lock.locked()})
        else:
            self._send_json(404, {"error": "not_found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/swap":
            self._send_json(404, {"error": "not_found"})
            return

        if not SWAP_WEBHOOK_KEY:
            self._send_json(503, {"error": "SWAP_WEBHOOK_KEY not configured"})
            return
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return

        params = parse_qs(parsed.query)
        profile_list = params.get("profile", [])
        profile = profile_list[0].strip() if profile_list else ""
        if profile not in VALID_PROFILES:
            self._send_json(400, {
                "error": "unknown_profile",
                "profile": profile,
                "valid": sorted(VALID_PROFILES),
            })
            return

        # Single-flight: if a swap is already running, return 409 immediately
        # rather than queueing. The router re-checks the active backend under its
        # own lock, so a queued-and-retried webhook call is safe but wasteful.
        if not _swap_lock.acquire(blocking=False):
            self._send_json(409, {
                "error": "swap_in_progress",
                "message": "another swap is already running; retry shortly",
            })
            return

        log.info("swap start: profile=%s", profile)
        result = None
        swap_error: dict | None = None
        try:
            result = subprocess.run(
                [SWAP_SCRIPT, profile],
                capture_output=True,
                text=True,
                timeout=SWAP_SUBPROC_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            log.error("swap timeout: profile=%s after %ds", profile, SWAP_SUBPROC_TIMEOUT)
            swap_error = {
                "error": "swap_timeout",
                "profile": profile,
                "message": f"swap-chat-model.sh did not finish within {SWAP_SUBPROC_TIMEOUT}s",
            }
        except Exception as e:
            log.error("swap exception: profile=%s %s", profile, e)
            swap_error = {"error": "swap_exception", "message": str(e)}
        finally:
            _swap_lock.release()

        if swap_error is not None:
            self._send_json(500, swap_error)
            return

        if result.returncode == 0:
            log.info("swap complete: profile=%s rc=0", profile)
            self._send_json(200, {
                "ok": True,
                "profile": profile,
                "output": result.stdout[-2000:],
            })
        else:
            log.error("swap failed: profile=%s rc=%d", profile, result.returncode)
            self._send_json(500, {
                "error": "swap_failed",
                "profile": profile,
                "returncode": result.returncode,
                "output": (result.stdout + result.stderr)[-2000:],
            })


# ---------- Entry point ----------

def main() -> None:
    if not SWAP_WEBHOOK_KEY:
        log.error("SWAP_WEBHOOK_KEY is not set in environment; refusing to start")
        sys.exit(1)
    if not os.path.isfile(SWAP_SCRIPT):
        log.error("SWAP_SCRIPT not found: %s", SWAP_SCRIPT)
        sys.exit(1)

    server = ThreadingHTTPServer((SWAP_WEBHOOK_BIND, SWAP_WEBHOOK_PORT), SwapHandler)
    log.info(
        "swap-webhook listening on %s:%d (script=%s)",
        SWAP_WEBHOOK_BIND, SWAP_WEBHOOK_PORT, SWAP_SCRIPT,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutdown requested")


if __name__ == "__main__":
    main()
