#!/usr/bin/env python3
"""Build curated URL lists for the untracked sdg-documentation content."""
import json, glob, re, collections, urllib.request, sys, os

KEY = sys.argv[1]
BASE = "http://192.168.6.154:3001/api/v1"

req = urllib.request.Request(BASE + "/workspace/sdg-documentation",
                             headers={"Authorization": "Bearer " + KEY})
w = json.load(urllib.request.urlopen(req, timeout=180)).get("workspace")
w = w[0] if isinstance(w, list) else w
docs = w.get("documents", [])

tracked = set()
for f in glob.glob("/tank/rag-state/*/documents.json"):
    for rec in json.load(open(f)).values():
        p = rec.get("allm_doc_path")
        if p:
            tracked.add(p)

def url_of(x):
    m = x.get("metadata")
    if isinstance(m, str):
        try: m = json.loads(m)
        except Exception: m = {}
    u = ((m or {}).get("url") or "")
    u = u.replace("file://", "").replace("web://", "")
    # AnythingLLM's link scraper appends ".website" to the stored URL. It is an
    # artifact -- fetching the suffixed form 404s -- so strip it here or the
    # whole curated list is unusable.
    if u.endswith(".website"):
        u = u[: -len(".website")]
    return u if u.startswith("http") else ""

untracked = []
for x in docs:
    if x.get("docpath") in tracked:
        continue
    u = url_of(x)
    if u:
        untracked.append(u)
untracked = list(dict.fromkeys(untracked))

# truenas-blog already HAS state (595 urls) but its rss source is disabled.
blog = []
try:
    blog = sorted(json.load(open("/tank/rag-state/truenas-blog/documents.json")).keys())
except Exception:
    pass

def by_host(u):
    m = re.match(r"https?://([^/]+)", u)
    return m.group(1) if m else "?"

# Interleave hosts so consecutive requests hit different sites. With N workers
# a host-sorted list would aim every concurrent request at one small blog;
# round-robin spreads the load without needing host-aware scheduling.
def interleave(urls):
    buckets = collections.OrderedDict()
    for u in urls:
        buckets.setdefault(by_host(u), []).append(u)
    out, i = [], 0
    while any(buckets.values()):
        for h in list(buckets):
            if buckets[h]:
                out.append(buckets[h].pop(0))
    return out

community = interleave([u for u in untracked if u not in set(blog)])
os.makedirs("/tank/rag-state/_urllists", exist_ok=True)
with open("/tank/rag-state/_urllists/sdg-community.txt", "w", encoding="utf-8") as fh:
    fh.write("# Curated list: sdg-documentation content with no owning source.\n")
    fh.write("# Built from the workspace on 2026-08-24. Hosts interleaved.\n")
    fh.write("\n".join(community) + "\n")
with open("/tank/rag-state/_urllists/truenas-blog.txt", "w", encoding="utf-8") as fh:
    fh.write("# truenas.com/blog posts, adopted from the disabled rss source.\n")
    fh.write("\n".join(interleave(blog)) + "\n")

print("  community urls :", len(community))
print("  truenas-blog   :", len(blog))
print("  community hosts:", dict(collections.Counter(by_host(u) for u in community).most_common(8)))
