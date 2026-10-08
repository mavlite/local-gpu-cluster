"""Reference solution for T6 (never copied into a worker's workspace)."""


class NotFound(Exception):
    pass


class MethodNotAllowed(Exception):
    def __init__(self, allowed):
        super().__init__(f"method not allowed; allowed: {allowed}")
        self.allowed = allowed


def _segments(path):
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return [] if path == "/" else path.lstrip("/").split("/")


def _compile(pattern):
    out = []
    for seg in _segments(pattern):
        if seg.startswith("{") and seg.endswith("}"):
            name, _, kind = seg[1:-1].partition(":")
            out.append(("int" if kind == "int" else "str", name))
        else:
            out.append(("lit", seg))
    return out


def _match(compiled, segs):
    if len(compiled) != len(segs):
        return None
    params = {}
    for (kind, val), seg in zip(compiled, segs):
        if kind == "lit":
            if seg != val:
                return None
        elif kind == "int":
            if not (seg.isascii() and seg.isdigit()):
                return None
            params[val] = int(seg)
        else:
            if not seg:
                return None
            params[val] = seg
    return params


def _rank(compiled):
    # literal (0) beats a parameter (1) at the left-most differing position
    return tuple(0 if kind == "lit" else 1 for kind, _ in compiled)


class Router:
    def __init__(self):
        self._routes = []                               # (method, pattern, compiled, handler)

    def add(self, method, pattern, handler):
        method = method.upper()
        if any(m == method and p == pattern for m, p, _, _ in self._routes):
            raise ValueError(f"duplicate route {method} {pattern}")
        self._routes.append((method, pattern, _compile(pattern), handler))

    def match(self, method, path):
        method, segs = method.upper(), _segments(path)
        hits = [(m, c, h, p) for m, _, c, h in self._routes
                if (p := _match(c, segs)) is not None]
        if not hits:
            raise NotFound(path)
        same = [x for x in hits if x[0] == method]
        if not same:
            raise MethodNotAllowed(sorted({m for m, *_ in hits}))
        _, _, handler, params = min(same, key=lambda x: _rank(x[1]))
        return handler, params
