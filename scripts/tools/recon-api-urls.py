"""Reconstruct api.truenas.com URLs from the slug-only documents' filenames."""
import json, re, collections
tracked = json.load(open("/tmp/tracked_map.json"))
d = json.load(open("/tmp/sdgws3.json"))
w = d.get("workspace"); w = w[0] if isinstance(w, list) else w
def meta(x):
    m = x.get("metadata")
    if isinstance(m, str):
        try: return json.loads(m)
        except Exception: return {}
    return m or {}
def url_of(x):
    u = (meta(x).get("url") or "")
    for p in ("web://", "file://"):
        if u.startswith(p): u = u[len(p):]
    if u.endswith(".website"): u = u[:-len(".website")]
    return u if u.startswith("http") else ""

slug_only = [x for x in w.get("documents", [])
             if x.get("docpath") not in tracked and not url_of(x)]

BASE = "https://api.truenas.com/v27.0/"
out, kinds = [], collections.Counter()
for x in slug_only:
    fn = x.get("docpath", "").rsplit("/", 1)[-1]
    fn = re.sub(r"^raw-", "", fn)
    fn = re.sub(r"-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.json$", "", fn)
    # "<method-with-dashes>-and8212-truenas-api-v27-0"  ->  method.name
    m = re.match(r"^(.*?)-and8212-truenas-api-v27-0$", fn)
    if not m:
        kinds["not an api page"] += 1
        continue
    slug = m.group(1)
    # dashes stand in for BOTH dots and underscores in the original method
    # name, which is ambiguous; try the dot reading first since method names
    # are dotted (pool.dataset.create) and underscores appear only inside a
    # segment (get_instance).
    kinds["api page"] += 1
    out.append((slug, BASE + "api_methods_" + slug.replace("-", ".") + ".html"))
print("  slug-only docs:", len(slug_only), dict(kinds))
json.dump(out, open("/tmp/api_recon.json", "w"))
print("  candidates written:", len(out))
for s, u in out[:4]:
    print("   ", u[:88])
