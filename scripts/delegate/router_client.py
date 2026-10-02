class InputTooLarge(Exception): ...
class ProfileBusy(Exception): ...

_ALIAS = {"code": "qwen3.8-think"}

def _alias(mode: str) -> str:
    return _ALIAS.get(mode, "rag-qwen3.8")

def _healthz_url(cfg) -> str:
    return cfg.router_url.rsplit("/v1", 1)[0] + "/healthz"

async def ask_local(http, cfg, *, prompt, content=None, files=None, mode="summarize", read_file=None):
    parts = []
    if content:
        parts.append(content)
    for path in (files or []):
        data = (read_file or _default_read)(path)
        parts.append(data.decode("utf-8", "replace"))
    joined = "".join(parts)
    if len(joined.encode("utf-8")) > cfg.ask_max_input_bytes:
        raise InputTooLarge(f"input exceeds {cfg.ask_max_input_bytes} bytes")

    hz = await http.get(_healthz_url(cfg))
    if (hz.json() or {}).get("active_chat_profile") != "qwen3.8":
        raise ProfileBusy("chat slot is not serving qwen3.8")

    messages = [{"role": "user", "content": (prompt + "\n\n" + joined) if joined else prompt}]
    body = {"model": _alias(mode), "messages": messages, "stream": False}
    headers = {"Authorization": f"Bearer {cfg.router_token}", "Content-Type": "application/json"}
    url = cfg.router_url.rstrip("/") + "/chat/completions"

    for attempt in range(2):
        r = await http.post(url, json=body, headers=headers)
        if r.status_code == 200:
            data = r.json()
            text = data["choices"][0]["message"].get("content", "")
            u = data.get("usage") or {}
            return {"text": text, "usage": {"input": u.get("prompt_tokens", 0),
                                            "output": u.get("completion_tokens", 0)}}
        if attempt == 0 and r.status_code in (502, 503):
            continue
        raise RuntimeError(f"router error {r.status_code}: {r.json()}")

def _default_read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()
