#!/usr/bin/env bash
# wf-sandbox-provision.sh -- runs INSIDE the workforce sandbox VM as root, in `build` firewall mode
# (internet, no private space). Pushed and invoked by scripts/76-vm-wf-sandbox.sh provision.
#
# Leaves the VM pre-warmed for offline measured runs (decision 2026-10-07: no package proxy):
#   * /opt/wfpy        Python venv with pytest + the repo's test deps; its freeze is the constraint
#                      set the grader image is built from, so agents and the grader run the same
#                      versions
#   * opencode         pinned version, real binary path recorded (not the npm shim)
#   * wf-grader:1      network-less grading image (Plan C DockerRunner)
#   * users            <prefix>-impl-1..3 and <prefix>-lead: no sudo, NOT in the docker group
#                      (docker group = root), login shell for opencode's bash tool
#   * /srv/wf          0711 root; bundles/ 0700 (hidden tests); runs/ 0711 (Plan C layout)
#   * /opt/workforce   the harness, 0700 root
#   * /usr/local/sbin/wf-run   the harness entry point with PATH and WF_OPENCODE set
# Prints one JSON summary on success.
set -Eeuo pipefail

PUSH="${WF_PUSH_DIR:-/root/wf-push}"
PREFIX="${WF_USER_PREFIX:-wf}"
OPENCODE_VERSION="${WF_OPENCODE_VERSION:-1.18.34}"
GRADER_IMAGE="${WF_GRADER_IMAGE:-wf-grader:1}"
export DEBIAN_FRONTEND=noninteractive

die() { echo "[provision] FATAL: $*" >&2; exit 1; }
log() { echo "[provision] $*" >&2; }
[[ $EUID -eq 0 ]] || die "run as root"
for f in workforce.tar wf-grader.Dockerfile requirements.txt; do
  [[ -s "$PUSH/$f" ]] || die "missing $PUSH/$f (push it first)"
done

log "packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv git shellcheck docker.io nodejs npm curl ca-certificates \
  procps util-linux >/dev/null

log "IPv6 off (the firewall policy is IPv4-only)"
printf 'net.ipv6.conf.all.disable_ipv6 = 1\nnet.ipv6.conf.default.disable_ipv6 = 1\n' \
  > /etc/sysctl.d/90-wf-no-ipv6.conf
sysctl -q --system

log "python venv + test deps (frozen for the grader image)"
python3 -m venv /opt/wfpy
/opt/wfpy/bin/pip install -q --upgrade pip
/opt/wfpy/bin/pip install -q pytest -r "$PUSH/requirements.txt"
/opt/wfpy/bin/pip freeze --exclude pip > "$PUSH/constraints.txt"
install -m 0644 "$PUSH/constraints.txt" /opt/wfpy/constraints.txt

log "opencode $OPENCODE_VERSION"
npm install -g --silent "opencode-ai@$OPENCODE_VERSION" >/dev/null
OC_BIN="$(npm root -g)/opencode-ai/bin/opencode"
[[ -x "$OC_BIN" ]] || OC_BIN="$(readlink -f "$(command -v opencode)")"
got="$("$OC_BIN" --version </dev/null | tr -d '[:space:]')"
[[ "$got" == "$OPENCODE_VERSION" ]] || die "opencode is $got, expected $OPENCODE_VERSION"

log "users"
for role in impl-1 impl-2 impl-3 lead; do
  u="$PREFIX-$role"
  id "$u" >/dev/null 2>&1 || useradd --system --create-home --shell /bin/bash "$u"
  if id -nG "$u" | tr ' ' '\n' | grep -qxE 'docker|sudo|adm'; then die "$u is in a privileged group"; fi
  chmod 0700 "$(getent passwd "$u" | cut -d: -f6)"       # agents cannot read each other's homes
done

log "lock the cloud-init admin and ssh (the guest agent is the only way in)"
if id wfadmin >/dev/null 2>&1; then passwd -l wfadmin >/dev/null; rm -f /etc/sudoers.d/90-cloud-init-users; fi
systemctl disable --now ssh.service ssh.socket >/dev/null 2>&1 || true

log "layout"
install -d -m 0711 -o root -g root /srv/wf /srv/wf/runs
install -d -m 0700 -o root -g root /srv/wf/bundles /opt/workforce
tar -x -C /opt/workforce --no-same-owner -f "$PUSH/workforce.tar"
chmod -R go-rwx /opt/workforce
[[ -f /opt/workforce/workforce/cli.py ]] || die "harness not unpacked at /opt/workforce/workforce"

cat > /usr/local/sbin/wf-run <<EOF
#!/bin/sh
# Harness entry point (root only): pinned python deps first on PATH, the real opencode binary.
export PATH=/opt/wfpy/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export WF_OPENCODE="$OC_BIN"
exec /opt/wfpy/bin/python3 /opt/workforce/workforce/cli.py "\$@"
EOF
chmod 0700 /usr/local/sbin/wf-run

log "grader image $GRADER_IMAGE"
systemctl enable --now docker >/dev/null 2>&1
ctx="$(mktemp -d)"
cp "$PUSH/wf-grader.Dockerfile" "$ctx/Dockerfile"
cp "$PUSH/constraints.txt" "$ctx/constraints.txt"
docker build -q -t "$GRADER_IMAGE" "$ctx" >/dev/null
rm -rf "$ctx"
docker run --rm --network none "$GRADER_IMAGE" python3 -m pytest --version >/dev/null 2>&1 \
  || die "grader image cannot run pytest"

cat > /etc/wf-sandbox.json <<EOF
{"opencode": "$got", "opencode_bin": "$OC_BIN", "python": "$(/opt/wfpy/bin/python3 -V | cut -d' ' -f2)",
 "pytest": "$(/opt/wfpy/bin/python3 -m pytest --version 2>&1 | awk '{print $2}')",
 "grader_image": "$GRADER_IMAGE", "grader_image_id": "$(docker image inspect -f '{{.Id}}' "$GRADER_IMAGE")",
 "users": ["$PREFIX-impl-1", "$PREFIX-impl-2", "$PREFIX-impl-3", "$PREFIX-lead"],
 "constraints_sha256": "$(sha256sum /opt/wfpy/constraints.txt | cut -d' ' -f1)"}
EOF
cat /etc/wf-sandbox.json
