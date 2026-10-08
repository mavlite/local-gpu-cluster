# T6 — URL path router

Implement `Router`, `NotFound` and `MethodNotAllowed` in `path_router.py`. Every rule below is
tested; nothing else is.

## `Router.add(method, pattern, handler)`
- `method` is case-insensitive and stored upper-case.
- `pattern` starts with `/`. Each segment between slashes is one of:
  - a **literal**, e.g. `users`;
  - `{name}`: matches any one non-empty segment and captures it as a `str`;
  - `{name:int}`: matches one segment of ASCII digits only and captures it as an `int`.
- Adding the same `(method, pattern)` twice raises `ValueError`.

## `Router.match(method, path)`
Returns `(handler, params)`, where `params` is a dict of the captured values.
- **Trailing slashes**: one trailing slash is ignored, so `/a/b/` matches `/a/b`. The root path `/`
  is just `/`.
- **Precedence**: if several patterns match, prefer the one with a **literal** at the
  **left-most position where they differ**. Example: for `/users/me`, the pattern `/users/me` wins
  over `/users/{id}`, whatever the order they were added in.
- **Errors**:
  - If no pattern matches the path for any method, raise `NotFound`.
  - If a pattern matches the path but not for this method, raise `MethodNotAllowed`. Its
    attribute `.allowed` is the **sorted list** of methods that would match the path.
- Both exception classes subclass `Exception`.
