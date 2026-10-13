"""vision_guard: what the router lets through to llama-server's multimodal path (coding-engine phase 1, follow-up B).

llama-server fetches http(s) image URLs itself, from inside the GPU container, so the router accepts images only as
data: URIs. Images are refused unless vision is enabled, are counted in admission, and video parts are refused
until the llama.cpp input format is verified. Messages without media pass through untouched."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import vision_guard as vg  # noqa: E402

PNG = "data:image/png;base64,iVBORw0KGgo="


def msgs(*parts, role="user"):
    return [{"role": role, "content": list(parts)}]


def img(url):
    return {"type": "image_url", "image_url": {"url": url}}


def txt(t):
    return {"type": "text", "text": t}


def test_text_only_messages_pass_and_count_zero_images():
    r = vg.check_media([{"role": "user", "content": "hi"}, *msgs(txt("a"))], vision_enabled=False)
    assert r.error is None and r.images == 0


def test_data_uri_images_are_counted_when_vision_is_enabled():
    r = vg.check_media(msgs(txt("what is this"), img(PNG), img(PNG)), vision_enabled=True)
    assert r.error is None and r.images == 2


def test_images_are_refused_when_vision_is_disabled():
    r = vg.check_media(msgs(img(PNG)), vision_enabled=False)
    assert r.error is not None and r.status == 400 and "vision" in r.error


@pytest.mark.parametrize("url", ["http://192.168.6.175:8006/x.png", "https://example.com/a.png", "file:///etc/passwd",
                                 "HTTP://evil/x.png", " https://x/y.png", "ftp://h/x.png", ""])
def test_non_data_image_urls_are_refused(url):
    r = vg.check_media(msgs(img(url)), vision_enabled=True)
    assert r.error is not None and r.status == 400 and "data:" in r.error


def test_a_data_uri_that_is_not_an_image_is_refused():
    r = vg.check_media(msgs(img("data:text/html;base64,PGgxPg==")), vision_enabled=True)
    assert r.error is not None and r.status == 400


def test_image_url_given_as_a_bare_string_is_handled():
    r = vg.check_media(msgs({"type": "image_url", "image_url": "https://x/y.png"}), vision_enabled=True)
    assert r.error is not None and r.status == 400
    r = vg.check_media(msgs({"type": "image_url", "image_url": PNG}), vision_enabled=True)
    assert r.error is None and r.images == 1


@pytest.mark.parametrize("kind", ["video_url", "input_video", "input_audio"])
def test_video_and_audio_parts_are_refused_for_now(kind):
    r = vg.check_media(msgs({"type": kind, kind: {"url": "data:video/mp4;base64,AAAA"}}), vision_enabled=True)
    assert r.error is not None and r.status == 400


def test_malformed_parts_do_not_crash():
    r = vg.check_media([{"role": "user", "content": [None, 3, "x", {"type": "image_url"}]}], vision_enabled=True)
    assert r.error is not None and r.status == 400       # an image part with no URL is refused, nothing raises
    r = vg.check_media([{"role": "user"}, {"content": None}, "junk"], vision_enabled=True)
    assert r.error is None and r.images == 0


def test_image_tokens_estimate_scales_with_count():
    assert vg.image_token_estimate(0, per_image=1280) == 0
    assert vg.image_token_estimate(3, per_image=1280) == 3840


def test_message_text_handles_string_list_and_none():
    assert vg.message_text({"content": "plain"}) == "plain"
    assert vg.message_text({"content": [txt("a"), img(PNG), txt("b")]}) == "a\nb"
    assert vg.message_text({"content": None}) == ""
    assert vg.message_text({}) == ""


# ---- review round 1: allowlist, unhashable types, top-level keys, Anthropic blocks, /v1/completions, bounded reads --

import asyncio  # noqa: E402


@pytest.mark.parametrize("part", [{"type": ["x"]}, {"type": {}}, {"type": 3}])
def test_unhashable_or_non_string_part_types_are_refused_not_crashed(part):
    r = vg.check_media(msgs(part), vision_enabled=True)
    assert r.error is not None and r.status == 400


@pytest.mark.parametrize("kind", ["image", "input_image", "image_file", "something_new"])
def test_unknown_part_types_are_refused(kind):
    r = vg.check_media(msgs({"type": kind, "url": "http://x/y.png"}), vision_enabled=True)
    assert r.error is not None and r.status == 400


def test_refusal_parts_in_assistant_messages_are_allowed():
    r = vg.check_media([{"role": "assistant", "content": [{"type": "refusal", "refusal": "no"}]}], vision_enabled=False)
    assert r.error is None


@pytest.mark.parametrize("key", ["multimodal_data", "image_data", "prompt_string"])
def test_llama_cpp_multimodal_top_level_keys_are_refused_on_chat(key):
    r = vg.check_chat_body({"model": "m", "messages": [], key: ["AAAA"]})
    assert r.error is not None and r.status == 400


def test_plain_chat_body_passes_the_key_check():
    assert vg.check_chat_body({"model": "m", "messages": [], "tools": [], "stream": True}).error is None


B64 = {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}


def ant(*blocks, role="user", system=None):
    body = {"model": "m", "max_tokens": 8, "messages": [{"role": role, "content": list(blocks)}]}
    if system is not None:
        body["system"] = system
    return body


def test_anthropic_text_tool_and_thinking_blocks_pass():
    body = {"model": "m", "max_tokens": 8, "system": [{"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": "plain"},
                         {"role": "assistant", "content": [{"type": "thinking", "thinking": "t", "signature": "x"},
                                                           {"type": "redacted_thinking", "data": "x"},
                                                           {"type": "tool_use", "id": "1", "name": "f", "input": {}}]},
                         {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1",
                                                       "content": [{"type": "text", "text": "r"}]}]}]}
    r = vg.check_media_anthropic(body, vision_enabled=False)
    assert r.error is None and r.images == 0


def test_anthropic_base64_image_counts_when_vision_on_and_is_refused_when_off():
    assert vg.check_media_anthropic(ant({"type": "image", "source": B64}), vision_enabled=True).images == 1
    off = vg.check_media_anthropic(ant({"type": "image", "source": B64}), vision_enabled=False)
    assert off.error is not None and off.status == 400


@pytest.mark.parametrize("source", [{"type": "url", "url": "http://192.168.6.175:8006/"},
                                    {"type": "file", "file_id": "f"},
                                    {"type": "base64", "media_type": "text/html", "data": "PGgxPg=="},
                                    {"type": "base64", "media_type": "image/png"},
                                    "http://x/y.png", None])
def test_anthropic_image_sources_other_than_base64_images_are_refused(source):
    r = vg.check_media_anthropic(ant({"type": "image", "source": source}), vision_enabled=True)
    assert r.error is not None and r.status == 400


def test_anthropic_url_image_nested_in_a_tool_result_is_refused():
    body = ant({"type": "tool_result", "tool_use_id": "1",
                "content": [{"type": "image", "source": {"type": "url", "url": "http://x/y.png"}}]})
    r = vg.check_media_anthropic(body, vision_enabled=True)
    assert r.error is not None and r.status == 400


@pytest.mark.parametrize("block", [{"type": "document", "source": {"type": "url", "url": "http://x/a.pdf"}},
                                   {"type": "search_result"}, {"type": ["text"]}, "junk"])
def test_anthropic_other_blocks_are_refused(block):
    r = vg.check_media_anthropic(ant(block), vision_enabled=True)
    assert r.error is not None and r.status == 400


def test_anthropic_non_text_system_block_is_refused():
    r = vg.check_media_anthropic(ant({"type": "text", "text": "hi"},
                                     system=[{"type": "image", "source": {"type": "url", "url": "http://x"}}]),
                                 vision_enabled=True)
    assert r.error is not None and r.status == 400


@pytest.mark.parametrize("body,ok", [({"prompt": "def f():"}, True), ({"prompt": ["a", "b"]}, True),
                                     ({"prompt": [1, 2, 3]}, True), ({"prompt": [[1, 2], [3]]}, True),
                                     ({"prompt": {"prompt_string": "x <__media__>", "multimodal_data": ["AA"]}}, False),
                                     ({"prompt": "x", "multimodal_data": ["AA"]}, False),
                                     ({"prompt": [{"x": 1}]}, False), ({"prompt": 5}, False)])
def test_completions_body_refuses_multimodal_and_odd_prompts(body, ok):
    r = vg.check_completions_body(body)
    assert (r.error is None) == ok


class FakeRequest:
    def __init__(self, chunks, content_length=None):
        self.headers = {} if content_length is None else {"content-length": str(content_length)}
        self._chunks = chunks
        self.pulled = 0

    async def stream(self):
        for c in self._chunks:
            self.pulled += 1
            yield c


def read(req, cap):
    return asyncio.run(vg.read_body_capped(req, cap))


def test_reader_returns_the_body_under_the_cap():
    assert read(FakeRequest([b"ab", b"cd"], 4), 10) == b"abcd"


def test_reader_rejects_by_content_length_before_reading():
    req = FakeRequest([b"x" * 100], 100)
    with pytest.raises(vg.BodyTooLarge):
        read(req, 10)
    assert req.pulled == 0


def test_reader_stops_a_chunked_body_at_the_cap():
    req = FakeRequest([b"x" * 6] * 100)               # no content-length, endless-ish stream
    with pytest.raises(vg.BodyTooLarge):
        read(req, 10)
    assert req.pulled == 2


@pytest.mark.parametrize("cl", ["abc", "-1", "1e9", "²", "٣"])
def test_reader_rejects_malformed_content_length(cl):
    req = FakeRequest([b"{}"])
    req.headers = {"content-length": cl}
    with pytest.raises(vg.BadRequestBody):
        read(req, 10)
