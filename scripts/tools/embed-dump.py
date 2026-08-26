"""Dump full embedding vectors to JSON, or compare two dumps by cosine similarity.

Vectors that shift between configs land in a different space from the ~21k
already stored in LanceDB, which degrades retrieval invisibly. Comparing five
printed dimensions cannot distinguish a real shift from float reduction-order
noise; cosine over all 1024 dims can.

    embed_dump.py dump  <out.json>
    embed_dump.py cmp   <a.json> <b.json>
"""
import json
import math
import os
import sys
import urllib.request

TEXTS = [
    "VMware Cloud Foundation 9.1 upgrade prerequisites and vSAN capacity planning",
    "NSX Edge cluster compatible version before SDDC Manager permits upgrade",
    "vSAN storage policy failures to tolerate and stripe width configuration",
]


def embed(text):
    req = urllib.request.Request(
        "http://127.0.0.1:8082/v1/embeddings",
        data=json.dumps({"input": text, "model": "qwen3-embed"}).encode(),
        headers={"Authorization": "Bearer " + os.environ["LLAMACPP_API_KEY"],
                 "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())["data"][0]["embedding"]


def cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb)


def main():
    mode = sys.argv[1]
    if mode == "dump":
        json.dump([embed(t) for t in TEXTS], open(sys.argv[2], "w"))
        print("    dumped %d vectors -> %s" % (len(TEXTS), sys.argv[2]))
        return
    a = json.load(open(sys.argv[2]))
    b = json.load(open(sys.argv[3]))
    for i, (x, y) in enumerate(zip(a, b)):
        c = cos(x, y)
        maxd = max(abs(p - q) for p, q in zip(x, y))
        print("    text %d: cosine=%.10f  max_abs_dim_delta=%.3e" % (i + 1, c, maxd))


main()
