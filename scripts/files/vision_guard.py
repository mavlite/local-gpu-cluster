"""vision_guard -- what the router lets through to llama-server's multimodal paths, and bounded body reads.

llama-server downloads an http(s) image URL itself, from inside the GPU container (LXC 151), which turns any client
into a fetch proxy for the cluster network. So, on every route that forwards user content:
  * OpenAI chat parts are an ALLOWLIST (text, refusal, image_url); an image_url must be a data:image/ URI and is
    refused while vision is disabled (no projector loaded); llama.cpp's own top-level multimodal keys are refused.
  * Anthropic /v1/messages blocks are an allowlist (text, thinking, redacted_thinking, tool_use, tool_result with the
    same rules inside, image with a base64 image/* source only).
  * /v1/completions refuses llama.cpp's multimodal prompt forms.
Bodies are read with a hard cap (Content-Length checked first, then a running total over the stream), so an
oversized or chunked upload is refused before it is buffered.

Pure functions plus one async reader; router-app.py maps refusals to HTTP errors. Tests: tests/test_vision_guard.py,
tests/test_router_vision.py.
"""
from dataclasses import dataclass
from typing import Optional

CHAT_PART_TYPES = {"text", "refusal", "image_url"}
CHAT_REFUSED_KEYS = {"multimodal_data", "image_data", "prompt_string"}
ANTHROPIC_PLAIN_BLOCKS = {"text", "thinking", "redacted_thinking", "tool_use"}


@dataclass(frozen=True)
class MediaCheck:
    images: int = 0
    error: Optional[str] = None
    status: int = 200


def _refuse(msg: str) -> MediaCheck:
    return MediaCheck(error=msg, status=400)


# ---- OpenAI chat ---------------------------------------------------------------------------------------------------

def _image_url(part: dict) -> str:
    raw = part.get("image_url")
    if isinstance(raw, dict):
        raw = raw.get("url")
    return raw if isinstance(raw, str) else ""


def check_media(messages, vision_enabled: bool) -> MediaCheck:
    """Walk OpenAI chat messages (every role). Returns the image count, or the first refusal (status 400)."""
    images = 0
    for m in messages if isinstance(messages, list) else []:
        content = m.get("content") if isinstance(m, dict) else None
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if not isinstance(kind, str) or kind not in CHAT_PART_TYPES:
                shown = kind if isinstance(kind, str) else type(kind).__name__
                return _refuse(f"content part type '{shown}' is not accepted by this router "
                               f"(allowed: {', '.join(sorted(CHAT_PART_TYPES))})")
            if kind != "image_url":
                continue
            if not vision_enabled:
                return _refuse("the loaded chat model has no vision projector; images are not accepted")
            if not _image_url(part).startswith("data:image/"):
                return _refuse("images must be sent inline as data: URIs (data:image/...;base64,...); "
                               "remote image URLs are not fetched")
            images += 1
    return MediaCheck(images=images)


def check_chat_body(body) -> MediaCheck:
    """Refuse llama.cpp's non-OpenAI top-level multimodal fields on /v1/chat/completions."""
    if not isinstance(body, dict):
        return _refuse("request body must be a JSON object")
    bad = sorted(k for k in body if k in CHAT_REFUSED_KEYS)
    if bad:
        return _refuse(f"fields not accepted by this router: {', '.join(bad)}")
    return MediaCheck()


# ---- Anthropic /v1/messages ----------------------------------------------------------------------------------------

def _anthropic_blocks(blocks, vision_enabled: bool, allow_tool_result: bool):
    """Return (images, refusal-or-None) for a list of Anthropic content blocks."""
    images = 0
    for b in blocks:
        if not isinstance(b, dict):
            return images, _refuse("content blocks must be JSON objects")
        kind = b.get("type")
        if not isinstance(kind, str):
            return images, _refuse(f"content block type '{type(kind).__name__}' is not accepted")
        if kind in ANTHROPIC_PLAIN_BLOCKS:
            continue
        if kind == "tool_result" and allow_tool_result:
            inner = b.get("content")
            if isinstance(inner, list):
                n, err = _anthropic_blocks(inner, vision_enabled, allow_tool_result=False)
                if err is not None:
                    return images, err
                images += n
            elif inner is not None and not isinstance(inner, str):
                return images, _refuse("tool_result content must be a string or a list of blocks")
            continue
        if kind == "image":
            if not vision_enabled:
                return images, _refuse("the loaded chat model has no vision projector; images are not accepted")
            src = b.get("source")
            ok = (isinstance(src, dict) and src.get("type") == "base64" and isinstance(src.get("data"), str)
                  and isinstance(src.get("media_type"), str) and src["media_type"].startswith("image/"))
            if not ok:
                return images, _refuse("images must be base64 sources with an image/* media_type; "
                                       "url and file sources are not fetched")
            images += 1
            continue
        return images, _refuse(f"content block type '{kind}' is not accepted by this router")
    return images, None


def check_media_anthropic(body, vision_enabled: bool) -> MediaCheck:
    if not isinstance(body, dict):
        return _refuse("request body must be a JSON object")
    system = body.get("system")
    if isinstance(system, list):
        for b in system:
            if not (isinstance(b, dict) and b.get("type") == "text"):
                return _refuse("system blocks must be text")
    elif system is not None and not isinstance(system, str):
        return _refuse("system must be a string or a list of text blocks")
    images = 0
    for m in body.get("messages") if isinstance(body.get("messages"), list) else []:
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, list):
            n, err = _anthropic_blocks(content, vision_enabled, allow_tool_result=True)
            if err is not None:
                return err
            images += n
    return MediaCheck(images=images)


# ---- /v1/completions ------------------------------------------------------------------------------------------------

def check_completions_body(body) -> MediaCheck:
    """Allow prompt as str | list[str] | list[int] | list[list[int]]; refuse llama.cpp's multimodal prompt forms."""
    if not isinstance(body, dict):
        return _refuse("request body must be a JSON object")
    bad = sorted(k for k in body if k in CHAT_REFUSED_KEYS)
    if bad:
        return _refuse(f"fields not accepted by this router: {', '.join(bad)}")
    p = body.get("prompt", "")
    ok = (isinstance(p, str)
          or (isinstance(p, list) and (all(isinstance(x, str) for x in p)
                                       or all(isinstance(x, int) and not isinstance(x, bool) for x in p)
                                       or all(isinstance(x, list) and all(isinstance(y, int) and not isinstance(y, bool)
                                                                          for y in x) for x in p))))
    return MediaCheck() if ok else _refuse("prompt must be a string, a list of strings, or token ids")


# ---- helpers shared with router-app.py -----------------------------------------------------------------------------

def image_token_estimate(images: int, per_image: int) -> int:
    """Admission estimate: each image may take up to the server's --image-max-tokens."""
    return max(0, images) * max(0, per_image)


def message_text(message) -> str:
    """The text of one message whatever its content shape (string, list of parts, missing)."""
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content
                         if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str))
    return ""


class BodyTooLarge(Exception):
    pass


class BadRequestBody(Exception):
    pass


async def read_body_capped(request, max_bytes: int) -> bytes:
    """Read a request body without ever holding more than max_bytes (+ one chunk). `request` needs .headers (with
    .get) and an async .stream() of bytes, as Starlette's Request has."""
    cl = request.headers.get("content-length")
    if cl is not None:
        if not (cl.isascii() and cl.isdigit()):   # '²'.isdigit() is True but int() rejects it
            raise BadRequestBody("malformed Content-Length")
        if int(cl) > max_bytes:
            raise BodyTooLarge()
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise BodyTooLarge()
        chunks.append(chunk)
    return b"".join(chunks)
