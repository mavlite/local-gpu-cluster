"""Post-swap end-to-end check: DB counts + one real RAG chat per workspace.
Reads ALLM_API_KEY from the host config file; never prints it."""
import json
import sqlite3
import time
import urllib.request

c = sqlite3.connect("file:/tank/anythingllm/storage/anythingllm.db?mode=ro", uri=True)
print("db: docs", c.execute("select count(*) from workspace_documents").fetchone()[0],
      "vectors", c.execute("select count(*) from document_vectors").fetchone()[0])

key = None
for line in open("/root/local-gpu-cluster/scripts/config.env"):
    if line.startswith("ALLM_API_KEY="):
        key = line.split("=", 1)[1].strip().strip('"').strip("'")
assert key, "no ALLM_API_KEY"


def chat(slug, msg):
    r = urllib.request.Request(
        f"http://192.168.6.154:3001/api/v1/workspace/{slug}/chat", method="POST",
        data=json.dumps({"message": msg, "mode": "query"}).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    t = time.time()
    d = json.load(urllib.request.urlopen(r, timeout=600))
    txt = (d.get("textResponse") or "").replace("\n", " ")
    srcs = d.get("sources") or []
    print(f"[{slug}] {time.time() - t:.0f}s sources={len(srcs)} error={d.get('error')}")
    print("   top source:", (srcs[0].get("title") if srcs else None))
    print("   ", txt[:280])


chat("vcf-reference", "What are the prerequisites before upgrading to VMware Cloud Foundation 9.1?")
chat("sdg-documentation", "How do I configure a ZFS pool scrub schedule in TrueNAS?")
