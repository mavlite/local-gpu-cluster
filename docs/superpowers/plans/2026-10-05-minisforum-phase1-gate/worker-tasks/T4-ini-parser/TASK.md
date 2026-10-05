# T4 — INI parser

Implement `parse`, `get_int` and `get_bool` in `ini_parse.py`. Every rule below is tested;
nothing else is. Line numbers are 1-based.

## `parse(text)`
Returns `dict[str, dict[str, str]]`.

**Line types.** Each line is processed in order:
- **Blank** (only whitespace): ignored.
- **Comment**: the first non-whitespace character is `#` or `;`. Ignored. Inline comments are
  **not** supported: `a = 1 # x` has the value `1 # x`.
- **Section header** `[name]`:
  - `name` is stripped of surrounding whitespace.
  - An empty name raises `ValueError`.
  - A section that appears twice raises `ValueError`.
- **Key/value**: `key = value` or `key: value`. The separator is whichever of `=` or `:` comes
  **first** in the line.
  - The key is stripped and **lower-cased**; the value is stripped.
  - An empty key raises `ValueError`.
  - A key repeated within the same section raises `ValueError`.
  - A key that appears before any section header belongs to the section `"DEFAULT"`.
- **Continuation**: a non-blank line that starts with whitespace, directly after a key/value line
  or another continuation. Its stripped text is appended to the previous value, joined with a
  single `"\n"`.
  - Continuation lines are recognised before comment and key/value lines, so an indented line
    containing `=` is still a continuation.
  - An indented line with no preceding key raises `ValueError`.
- **Anything else** raises `ValueError`.

**Errors.** Every `ValueError` raised by `parse` must contain the text `line N`, where `N` is the
offending line's number.

## Getters
- `get_int(cfg, section, key, default=None)` returns `int(value)`.
- `get_bool(cfg, section, key, default=None)` accepts, case-insensitively:
  - `1`, `yes`, `true`, `on` → `True`;
  - `0`, `no`, `false`, `off` → `False`;
  - anything else raises `ValueError`.
- Both look the key up lower-cased.
- If the section or key is missing, they return `default`. If `default` is `None`, they raise
  `KeyError` instead.
