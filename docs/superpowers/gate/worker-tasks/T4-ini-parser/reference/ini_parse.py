"""Reference solution for T4 (never copied into a worker's workspace)."""

_TRUE = {"1", "yes", "true", "on"}
_FALSE = {"0", "no", "false", "off"}


def parse(text):
    cfg = {}
    section = None
    last = None                                   # (section_name, key) of the last value line
    for n, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        if raw[0] in " \t":
            if last is None:
                raise ValueError(f"line {n}: continuation without a key")
            s, k = last
            cfg[s][k] = cfg[s][k] + "\n" + raw.strip()
            continue
        line = raw.strip()
        if line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            if not name:
                raise ValueError(f"line {n}: empty section name")
            if name in cfg:
                raise ValueError(f"line {n}: duplicate section {name!r}")
            cfg[name] = {}
            section, last = name, None
            continue
        seps = [i for i in (line.find("="), line.find(":")) if i != -1]
        if not seps:
            raise ValueError(f"line {n}: not a section, key or comment")
        i = min(seps)
        key, value = line[:i].strip().lower(), line[i + 1:].strip()
        if not key:
            raise ValueError(f"line {n}: empty key")
        target = section if section is not None else "DEFAULT"
        cfg.setdefault(target, {})
        if key in cfg[target]:
            raise ValueError(f"line {n}: duplicate key {key!r}")
        cfg[target][key] = value
        last = (target, key)
    return cfg


def _lookup(cfg, section, key, default):
    try:
        return cfg[section][key.lower()]
    except KeyError:
        if default is None:
            raise
        return None


def get_int(cfg, section, key, default=None):
    v = _lookup(cfg, section, key, default)
    return default if v is None else int(v)


def get_bool(cfg, section, key, default=None):
    v = _lookup(cfg, section, key, default)
    if v is None:
        return default
    low = v.lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    raise ValueError(f"not a boolean: {v!r}")
