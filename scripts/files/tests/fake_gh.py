"""Minimal stand-in for the `gh` CLI used by agent_runner_dispatch tests.

State lives in the JSON file named by $FAKE_GH_STATE:
  {"user": "mavlite", "issues": {"OWNER/REPO#N": {number, title, body, createdAt, labels: [..], comments: [..]}},
   "calls": [argv, ...]}
Only the subcommands the dispatcher (and the fake agent) use are implemented.
"""
import datetime
import json
import os
import sys


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def opt(args, *names, multi=False):
    vals = []
    for i, a in enumerate(args):
        if a in names and i + 1 < len(args):
            vals.append(args[i + 1])
    return vals if multi else (vals[-1] if vals else None)


def main(argv):
    path = os.environ["FAKE_GH_STATE"]
    with open(path, encoding="utf-8") as f:
        st = json.load(f)
    st.setdefault("calls", []).append(argv)
    out = None
    if argv[:2] == ["api", "user"]:
        out = st.get("user", "mavlite")
    elif argv == ["api", "-i", "rate_limit"]:
        # FAKE_GH_TOKEN_EXPIRY: the fine-grained token's expiry header, as GitHub sends it ("" = no header)
        exp = os.environ.get("FAKE_GH_TOKEN_EXPIRY", "")
        out = "HTTP/2.0 200 OK
" + (f"Github-Authentication-Token-Expiration: {exp}
" if exp else "") + "
{}"
    elif argv[:1] == ["issue"]:
        sub, args = argv[1], argv[2:]
        repo = opt(args, "-R", "--repo")
        if sub == "list":
            label = opt(args, "-l", "--label")
            rows = [i for k, i in st["issues"].items()
                    if k.startswith(repo + "#") and i.get("state", "OPEN") == "OPEN"
                    and (label is None or label in i["labels"])]
            out = json.dumps([{"number": i["number"], "title": i["title"], "body": i["body"],
                               "createdAt": i["createdAt"], "labels": [{"name": n} for n in i["labels"]]}
                              for i in rows])
        else:
            num = args[0]
            issue = st["issues"][f"{repo}#{num}"]
            if sub == "edit":
                # FAKE_GH_EDIT_FAIL=N: the first N edits apply only their removals and then fail, as GitHub's
                # GraphQL "Something went wrong" did on SDG-ARMA3#1 (2026-10-30).
                failing = st.get("edit_failures", 0) < int(os.environ.get("FAKE_GH_EDIT_FAIL", "0"))
                for group in opt(args, "--remove-label", multi=True):
                    for n in group.split(","):
                        if n in issue["labels"]:
                            issue["labels"].remove(n)
                if failing:
                    st["edit_failures"] = st.get("edit_failures", 0) + 1
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(st, f)
                    print("GraphQL: Something went wrong while executing your query.", file=sys.stderr)
                    return 1
                for group in opt(args, "--add-label", multi=True):
                    for n in group.split(","):
                        if n not in issue["labels"]:
                            issue["labels"].append(n)
            elif sub == "comment":
                body = opt(args, "--body", "-b")
                bf = opt(args, "--body-file", "-F")
                if bf:
                    with open(bf, encoding="utf-8") as f:
                        body = f.read()
                issue["comments"].append({"author": {"login": st.get("user", "mavlite")},
                                          "body": body, "createdAt": now()})
            elif sub == "view":
                out = json.dumps({"labels": [{"name": n} for n in issue["labels"]],
                                  "comments": issue["comments"]})
            else:
                print(f"fake gh: unsupported issue {sub}", file=sys.stderr)
                return 2
    else:
        print(f"fake gh: unsupported {argv}", file=sys.stderr)
        return 2
    with open(path, "w", encoding="utf-8") as f:
        json.dump(st, f)
    if out is not None:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
