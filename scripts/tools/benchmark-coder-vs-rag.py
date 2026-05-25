#!/usr/bin/env python3
"""benchmark-coder-vs-rag.py — A/B test the local chat model vs hosted Qwen3-Coder-Next.

Sends the same prompts to:
  - LOCAL: the cluster router's chat completions endpoint (currently
    Qwen3.6-35B-A3B). Reads ROUTER_API_KEY from env.
  - HOSTED: Qwen3-Coder-Next via HuggingFace Inference Providers
    (auto-routes to Novita / other providers). Reads HF_TOKEN from env.

Writes a side-by-side markdown report to /tmp/coder-benchmark-<ts>.md by
default. Use this to decide whether to swap the cluster's chat model to
Coder-Next BEFORE committing local VRAM to the 38.4 GB UD-IQ4_XS download.

Usage:
    ROUTER_API_KEY=$(pct exec 153 -- awk -F= '/^ROUTER_API_KEY=/{print $2}' /etc/router.env)
    export HF_TOKEN=hf_...   # https://huggingface.co/settings/tokens
    python3 scripts/tools/benchmark-coder-vs-rag.py

Custom prompts (JSON file with [{name, system, user}, ...]):
    python3 scripts/tools/benchmark-coder-vs-rag.py --prompts /tmp/my-prompts.json

Cost note: HF Inference Providers charges per token. 7 prompts × ~1500
output tokens × 2 (in+out) = ~21K tokens. At typical $0.20-0.50 per
million tokens for serverless Qwen models, the run costs cents.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

import requests


DEFAULT_PROMPTS = [
    {
        "name": "01-async-fetch-with-retry",
        "system": "You are an expert Python developer. Provide working, idiomatic code.",
        "user": (
            "Write a Python async function that fetches paginated API results "
            "from a generic JSON API (assume the response has a 'results' list "
            "and a 'next' field that is the URL of the next page, or null on "
            "the last page). Handle 429 with exponential backoff (max 5 retries, "
            "1s/2s/4s/8s/16s). Return all results as a list. Include type hints, "
            "a docstring, and a brief usage example."
        ),
    },
    {
        "name": "02-debug-from-traceback",
        "system": "You are a senior debugger. Diagnose precisely and minimally.",
        "user": (
            "A user reports this error from our service:\n\n"
            "```\n"
            "Traceback (most recent call last):\n"
            '  File "/app/router.py", line 287, in get_tokens\n'
            '    return r.json().get("tokens", [])\n'
            '  File "/usr/local/lib/python3.12/site-packages/httpx/_models.py", line 766, in json\n'
            "    return jsonlib.loads(self.text, **kwargs)\n"
            "ValueError: Expecting value: line 1 column 1 (char 0)\n"
            "```\n\n"
            "Here is the function:\n\n"
            "```python\n"
            "async def get_tokens(client, upstream, text):\n"
            "    r = await client.post(f'{upstream}/tokenize', json={'content': text}, timeout=5.0)\n"
            "    return r.json().get('tokens', [])\n"
            "```\n\n"
            "What's the bug, and what's the minimal fix? Don't write the full "
            "fixed function — just point at the line and explain."
        ),
    },
    {
        "name": "03-keycloak-saml-sp",
        "system": "You are a senior identity engineer who works with Keycloak daily.",
        "user": (
            "Show me how to set up a Keycloak SAML SP (Service Provider) client "
            "via the admin REST API using python-keycloak v3.x. I want to "
            "register a new SP, fetch the SP metadata XML, and write a minimal "
            "Flask route that initiates SP-initiated SSO. Just the essential "
            "code with brief inline commentary."
        ),
    },
    {
        "name": "04-bash-largest-files",
        "system": "Concise senior sysadmin.",
        "user": (
            "Write a single bash function `top_largest` that takes a directory "
            "path argument and prints the 5 largest files inside it (recursive). "
            "Output format: `<human-readable-size>\\t<path>`, one per line, "
            "sorted descending by size. Handle directories and filenames with "
            "spaces correctly. Show the function definition and one example call."
        ),
    },
    {
        "name": "05-multi-file-refactor",
        "system": "Senior engineer. Edit carefully — preserve behavior when possible.",
        "user": (
            "Given these two Python files:\n\n"
            "```python\n# auth.py\n"
            "def verify_token(token: str) -> bool:\n"
            '    return token == "hardcoded-secret"\n'
            "```\n\n"
            "```python\n# server.py\n"
            "from auth import verify_token\n\n"
            "def handler(request):\n"
            '    if not verify_token(request.headers.get("X-Token")):\n'
            '        return {"status": 401}\n'
            '    return {"status": 200, "data": "hello"}\n\n'
            "def admin_handler(request):\n"
            '    if not verify_token(request.headers.get("X-Token")):\n'
            '        return {"status": 401}\n'
            '    return {"status": 200, "data": "admin"}\n'
            "```\n\n"
            "Refactor verify_token to take a required `role` parameter "
            "(values: 'user' or 'admin'). The token store now uses different "
            "secrets per role — invent reasonable token values. Update both "
            "files. Output complete files."
        ),
    },
    {
        "name": "06-architecture-explain",
        "system": "You are a thoughtful staff engineer explaining design to a junior peer.",
        "user": (
            "Explain this code:\n\n"
            "```python\n"
            "class CircuitBreaker:\n"
            "    def __init__(self, failure_threshold=5, reset_timeout=60):\n"
            "        self.failure_count = 0\n"
            "        self.failure_threshold = failure_threshold\n"
            "        self.last_failure_time = None\n"
            "        self.reset_timeout = reset_timeout\n"
            "        self.state = 'closed'\n\n"
            "    async def call(self, fn):\n"
            "        if self.state == 'open':\n"
            "            if time.time() - self.last_failure_time > self.reset_timeout:\n"
            "                self.state = 'half-open'\n"
            "            else:\n"
            "                raise RuntimeError('circuit open')\n"
            "        try:\n"
            "            result = await fn()\n"
            "            if self.state == 'half-open':\n"
            "                self.state = 'closed'\n"
            "                self.failure_count = 0\n"
            "            return result\n"
            "        except Exception:\n"
            "            self.failure_count += 1\n"
            "            self.last_failure_time = time.time()\n"
            "            if self.failure_count >= self.failure_threshold:\n"
            "                self.state = 'open'\n"
            "            raise\n"
            "```\n\n"
            "Cover: (1) the pattern's name and purpose, (2) the three states "
            "and transitions, (3) the main tradeoffs vs alternatives, "
            "(4) one realistic failure mode this implementation doesn't handle."
        ),
    },
    {
        "name": "07-python-to-rust-axum",
        "system": "Idiomatic translator. Match behavior exactly. Output ready-to-compile code.",
        "user": (
            "Translate this Python FastAPI route to Rust + Axum 0.7. "
            "Match HTTP semantics exactly (same status codes, same JSON shape).\n\n"
            "```python\n"
            "@app.post('/process')\n"
            "async def process(payload: dict):\n"
            "    items = payload.get('items', [])\n"
            "    if not items:\n"
            "        raise HTTPException(status_code=400, detail='items required')\n"
            "    total = sum(item.get('value', 0) for item in items)\n"
            "    return {'total': total, 'count': len(items)}\n"
            "```\n\n"
            "Output a complete `main.rs` with the Axum router setup, handler, "
            "and necessary `use` statements. Include the Cargo.toml dependency "
            "lines as a comment at the top."
        ),
    },
]


def _chat(url: str, headers: dict, payload: dict, timeout: int = 300) -> dict:
    """Single chat completion request. Returns dict with content, duration, tokens.
    On any error, returns dict with content='ERROR: ...' and zeros for the rest."""
    started = time.monotonic()
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except requests.RequestException as e:
        return {
            "content": f"ERROR (network): {type(e).__name__}: {e}",
            "duration_s": time.monotonic() - started,
            "tokens_in": 0, "tokens_out": 0,
        }
    duration = time.monotonic() - started
    if r.status_code >= 400:
        return {
            "content": f"ERROR (HTTP {r.status_code}): {r.text[:500]}",
            "duration_s": duration,
            "tokens_in": 0, "tokens_out": 0,
        }
    try:
        data = r.json()
    except ValueError:
        return {
            "content": f"ERROR (invalid JSON response): {r.text[:500]}",
            "duration_s": duration,
            "tokens_in": 0, "tokens_out": 0,
        }
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = msg.get("content") or ""
    usage = data.get("usage") or {}
    return {
        "content": content,
        "duration_s": duration,
        "tokens_in": usage.get("prompt_tokens", 0),
        "tokens_out": usage.get("completion_tokens", 0),
    }


def call_local(prompt: dict, router_url: str, router_key: str, model: str) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt["system"]},
            {"role": "user", "content": prompt["user"]},
        ],
        "max_tokens": 1500,
        "temperature": 0.3,
        "top_p": 0.95,
    }
    headers = {
        "Authorization": f"Bearer {router_key}",
        "Content-Type": "application/json",
    }
    return _chat(f"{router_url}/v1/chat/completions", headers, payload)


def call_hosted(prompt: dict, hf_token: str, model: str) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt["system"]},
            {"role": "user", "content": prompt["user"]},
        ],
        "max_tokens": 1500,
        "temperature": 0.3,
        "top_p": 0.95,
    }
    headers = {
        "Authorization": f"Bearer {hf_token}",
        "Content-Type": "application/json",
    }
    return _chat(
        "https://router.huggingface.co/v1/chat/completions",
        headers, payload,
    )


def write_report(path: str, results: list, args) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Qwen3.6-35B-A3B (local) vs Qwen3-Coder-Next (hosted) — A/B benchmark\n\n")
        f.write(f"- **Date:** {datetime.now().isoformat(timespec='seconds')}\n")
        f.write(f"- **Local model:** `{args.local_model}` via `{args.router_url}`\n")
        f.write(f"- **Hosted model:** `{args.hf_model}` via HuggingFace Inference Providers\n")
        f.write(f"- **Prompts:** {len(results)}\n")
        f.write("- **Sampling:** `temperature=0.3, top_p=0.95, max_tokens=1500` for both\n\n")

        # Summary table at the top so reviewer sees overall stats first
        local_total_time = sum(r["local"]["duration_s"] for r in results) or 1
        hosted_total_time = sum(r["hosted"]["duration_s"] for r in results) or 1
        local_total_out = sum(r["local"]["tokens_out"] for r in results)
        hosted_total_out = sum(r["hosted"]["tokens_out"] for r in results)
        f.write("## Summary\n\n")
        f.write("| Endpoint | Total wall-clock | Total output tokens | Avg tokens/sec |\n")
        f.write("|---|---|---|---|\n")
        f.write(f"| Local `{args.local_model}` | {local_total_time:.1f}s | {local_total_out} | {local_total_out / local_total_time:.1f} |\n")
        f.write(f"| Hosted `{args.hf_model}` | {hosted_total_time:.1f}s | {hosted_total_out} | {hosted_total_out / hosted_total_time:.1f} |\n\n")
        f.write("**Reviewer instructions:** for each prompt below, read both "
                "responses and judge: (a) correctness, (b) idiomaticity / code "
                "quality, (c) completeness vs the ask. Note your verdict per "
                "prompt; tally at the end. Tokens/sec is a useful secondary "
                "signal but not the main decision factor.\n\n---\n\n")

        for r in results:
            p = r["prompt"]
            f.write(f"## {p['name']}\n\n")
            f.write("<details><summary>Prompt</summary>\n\n")
            f.write(f"**system:** {p['system']}\n\n")
            f.write(f"**user:**\n\n{p['user']}\n\n</details>\n\n")

            local = r["local"]
            hosted = r["hosted"]
            f.write(f"### Local `{args.local_model}` — {local['duration_s']:.1f}s, "
                    f"in={local['tokens_in']}, out={local['tokens_out']}\n\n")
            f.write(local["content"] + "\n\n")
            f.write(f"### Hosted `{args.hf_model}` — {hosted['duration_s']:.1f}s, "
                    f"in={hosted['tokens_in']}, out={hosted['tokens_out']}\n\n")
            f.write(hosted["content"] + "\n\n")
            f.write("**Your verdict:** _(local wins / hosted wins / tie / both wrong)_\n\n---\n\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompts", help="JSON file with custom prompts (default: built-in)")
    parser.add_argument("--out", help="Output markdown path (default: /tmp/coder-benchmark-<ts>.md)")
    parser.add_argument("--router-url", default=os.environ.get("ROUTER_URL", "http://192.168.6.153:8000"))
    parser.add_argument("--local-model", default="qwen3.6-think",
                        help="Model alias served by the router (default: qwen3.6-think)")
    parser.add_argument("--hf-model", default="Qwen/Qwen3-Coder-Next",
                        help="HF Inference Providers model id (default: Qwen/Qwen3-Coder-Next)")
    args = parser.parse_args()

    hf_token = os.environ.get("HF_TOKEN")
    if not hf_token:
        print("ERROR: HF_TOKEN env var required.", file=sys.stderr)
        print("Create one at https://huggingface.co/settings/tokens with Inference Providers permission.", file=sys.stderr)
        return 2

    router_key = os.environ.get("ROUTER_API_KEY")
    if not router_key:
        print("ERROR: ROUTER_API_KEY env var required.", file=sys.stderr)
        print("Fetch with:  pct exec 153 -- awk -F= '/^ROUTER_API_KEY=/{print $2}' /etc/router.env", file=sys.stderr)
        return 2

    if args.prompts:
        with open(args.prompts) as f:
            prompts = json.load(f)
    else:
        prompts = DEFAULT_PROMPTS

    if not args.out:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        args.out = f"/tmp/coder-benchmark-{ts}.md"

    print(f"Output:        {args.out}")
    print(f"Prompts:       {len(prompts)}")
    print(f"Local model:   {args.local_model} via {args.router_url}")
    print(f"Hosted model:  {args.hf_model} via HF Inference Providers")
    print()

    results = []
    for i, prompt in enumerate(prompts, 1):
        print(f"[{i}/{len(prompts)}] {prompt['name']}")
        print(f"  → local  ({args.local_model}) ...", end="", flush=True)
        local = call_local(prompt, args.router_url, router_key, args.local_model)
        ok = "OK" if not local["content"].startswith("ERROR") else "FAIL"
        print(f" {ok} ({local['duration_s']:.1f}s, {local['tokens_out']} tok)")

        print(f"  → hosted ({args.hf_model}) ...", end="", flush=True)
        hosted = call_hosted(prompt, hf_token, args.hf_model)
        ok = "OK" if not hosted["content"].startswith("ERROR") else "FAIL"
        print(f" {ok} ({hosted['duration_s']:.1f}s, {hosted['tokens_out']} tok)")

        results.append({"prompt": prompt, "local": local, "hosted": hosted})

    write_report(args.out, results, args)
    print(f"\nReport written: {args.out}")
    print("Review the markdown side-by-side, score each prompt, and decide whether")
    print("to deploy Coder-Next locally via the swap procedure in day-2-ops.md §4.4.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
