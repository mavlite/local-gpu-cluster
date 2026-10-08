import pytest

from ini_parse import get_bool, get_int, parse

DOC = """\
# top comment
name = default-name
[server]
Host = example.org
port: 8080
url = http://x:1/a=b
  ; indented comment-looking text is a continuation
debug = on
[ empty values ]
k =
"""


def test_sections_keys_and_default_section():
    cfg = parse(DOC)
    assert cfg["DEFAULT"] == {"name": "default-name"}
    assert cfg["server"]["host"] == "example.org"
    assert cfg["server"]["port"] == "8080"
    assert cfg["empty values"] == {"k": ""}


def test_first_separator_wins_and_continuation():
    cfg = parse(DOC)
    # '=' comes before ':' on that line, so the key is 'url' and the value keeps 'http://x:1/a=b';
    # the indented next line is a continuation even though it looks like a comment.
    assert cfg["server"]["url"] == "http://x:1/a=b\n; indented comment-looking text is a continuation"


def test_colon_first_separator():
    cfg = parse("[s]\na: b = c\n")
    assert cfg["s"] == {"a": "b = c"}


def test_inline_comment_is_value():
    assert parse("[s]\na = 1 # x\n")["s"]["a"] == "1 # x"


@pytest.mark.parametrize("text,line", [
    ("[a]\n[a]\n", 2),
    ("[a]\nx=1\nX = 2\n", 3),
    ("[]\n", 1),
    ("[a]\njust words\n", 2),
    ("  orphan continuation\n", 1),
    ("[a]\n= v\n", 2),
    ("[a]\nk = v\n[b]\n  stray\n", 4),
])
def test_errors_name_the_line(text, line):
    with pytest.raises(ValueError, match=rf"line {line}\b"):
        parse(text)


def test_getters():
    cfg = parse(DOC)
    assert get_int(cfg, "server", "PORT") == 8080
    assert get_bool(cfg, "server", "debug") is True
    assert get_bool(parse("[s]\nf = NO\n"), "s", "f") is False
    assert get_int(cfg, "server", "missing", 7) == 7
    assert get_bool(cfg, "nosection", "x", False) is False
    with pytest.raises(KeyError):
        get_int(cfg, "server", "missing")
    with pytest.raises(ValueError):
        get_bool(parse("[s]\nf = maybe\n"), "s", "f")
