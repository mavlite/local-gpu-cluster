"""wf-runner-grade.py BUNDLES_DIR PATCHES_DIR -- grade each task's patch on a runner, print {task: pass} as JSON.

Runs inside a runner VM with the harness at /opt/workforce/workforce and the wf-grader:1 image (network-less
Docker), i.e. exactly how sandbox VM 176 grades. Used by 79-vm-wf-runner-image.sh parity to compare a runner with
VM 176 on a harvested run. A task whose patch is missing is reported as False.
"""
import json
import os
import shutil
import sys

sys.path.insert(0, os.environ.get("WF_HARNESS", "/opt/workforce/workforce"))
import grade  # noqa: E402


def main(bundles, patches, work="/srv/wf/grading/work", image="wf-grader:1"):
    runner = grade.DockerRunner(image)
    out = {}
    for tid in sorted(os.listdir(bundles)):
        bundle = os.path.join(bundles, tid)
        if not os.path.isdir(bundle):
            continue
        pfile = os.path.join(patches, f"{tid}.patch")
        if not os.path.exists(pfile):
            out[tid] = False
            continue
        with open(pfile, "rb") as f:
            patch = f.read()
        wd = os.path.join(work, tid)
        shutil.rmtree(wd, ignore_errors=True)
        os.makedirs(wd)
        g = grade.grade(bundle, patch, wd, runner)
        out[tid] = bool(g.get("pass"))
        shutil.rmtree(wd, ignore_errors=True)
    return out


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1], sys.argv[2])))
