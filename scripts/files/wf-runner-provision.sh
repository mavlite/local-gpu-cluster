#!/usr/bin/env bash
# wf-runner-provision.sh -- runs INSIDE the runner build VM (VMID 177, Debian 13 generic) as root, with internet.
# Pushed and invoked by scripts/79-vm-wf-runner-image.sh. Coding engine phase 2
# (docs/superpowers/plans/2026-10-13-coding-engine-phase2-runners.md).
#
# A runner grades and executes tests for the background queue; it never hosts agents. It must grade exactly like
# sandbox VM 176, so the Python set is the sandbox venv's own constraints.txt (pushed, never re-resolved) and the
# grader image is built from the same Dockerfile under the same tag. The VM is then templated for the Minisforum
# hosts, where VLAN 10 has no internet: everything a runner needs is installed here.
#
# Inputs in $PUSH: constraints.txt (from VM 176 /opt/wfpy/constraints.txt), requirements.txt,
# extra-requirements.txt, wf-grader.Dockerfile, workforce.tar (the harness, for its grade module).
# Leaves: /opt/wfpy, wf-grader:1, pwsh + Pester, shellcheck, ripgrep, open-vm-tools, cloud-init (VMware datasource),
# /opt/workforce, ssh disabled, cloud-init state + machine-id cleared. Prints /etc/wf-runner.json.
set -Eeuo pipefail
PUSH="${WF_PUSH_DIR:-/root/wf-push}"
GRADER_IMAGE="${WF_GRADER_IMAGE:-wf-grader:1}"
PWSH_VERSION="${WF_PWSH_VERSION:-7.6.6}"
export DEBIAN_FRONTEND=noninteractive

die() { echo "[runner] FATAL: $*" >&2; exit 1; }
log() { echo "[runner] $*" >&2; }
[[ $EUID -eq 0 ]] || die "run as root"
for f in constraints.txt requirements.txt extra-requirements.txt wf-grader.Dockerfile workforce.tar; do
  [[ -s "$PUSH/$f" ]] || die "missing $PUSH/$f (push it first)"
done

log "packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv git shellcheck ripgrep docker.io open-vm-tools cloud-init \
  curl ca-certificates procps util-linux libicu-dev >/dev/null
command -v rg >/dev/null || die "ripgrep did not install"
command -v vmtoolsd >/dev/null || die "open-vm-tools did not install"

log "python venv pinned to the sandbox's constraints"
python3 -m venv /opt/wfpy
/opt/wfpy/bin/pip install -q --upgrade pip
/opt/wfpy/bin/pip install -q -c "$PUSH/constraints.txt" pytest -r "$PUSH/requirements.txt" -r "$PUSH/extra-requirements.txt"
install -m 0644 "$PUSH/constraints.txt" /opt/wfpy/constraints.txt

log "PowerShell $PWSH_VERSION + Pester"
url="https://github.com/PowerShell/PowerShell/releases/download/v${PWSH_VERSION}/powershell-${PWSH_VERSION}-linux-x64.tar.gz"
tmp="$(mktemp -d)"
curl -fsSL -o "$tmp/pwsh.tgz" "$url" || die "cannot download $url"
install -d /opt/microsoft/powershell/7
tar -xzf "$tmp/pwsh.tgz" -C /opt/microsoft/powershell/7
chmod +x /opt/microsoft/powershell/7/pwsh
ln -sf /opt/microsoft/powershell/7/pwsh /usr/local/bin/pwsh
rm -rf "$tmp"
pwsh -NoProfile -Command "Set-PSRepository PSGallery -InstallationPolicy Trusted; Install-Module Pester -Scope AllUsers -Force -SkipPublisherCheck" \
  || die "Pester install failed"
pester="$(pwsh -NoProfile -Command "(Get-Module -ListAvailable Pester | Sort-Object Version -Descending | Select-Object -First 1).Version.ToString()")"
[[ -n "$pester" ]] || die "Pester not found after install"

log "harness (grade module only; no agent tooling on runners)"
install -d -m 0700 -o root -g root /opt/workforce /srv/wf /srv/wf/bundles /srv/wf/grading
tar -x -C /opt/workforce --no-same-owner -f "$PUSH/workforce.tar"
[[ -f /opt/workforce/workforce/grade.py ]] || die "harness not unpacked at /opt/workforce/workforce"

log "grader image $GRADER_IMAGE"
systemctl enable --now docker >/dev/null 2>&1
ctx="$(mktemp -d)"
cp "$PUSH/wf-grader.Dockerfile" "$ctx/Dockerfile"
cp "$PUSH/constraints.txt" "$ctx/constraints.txt"
docker build -q -t "$GRADER_IMAGE" "$ctx" >/dev/null
rm -rf "$ctx"
docker run --rm --network none "$GRADER_IMAGE" python3 -m pytest --version >/dev/null 2>&1 \
  || die "grader image cannot run pytest"

log "no ssh: guest operations (open-vm-tools) are the control path, as for the llmbench workers"
systemctl disable --now ssh.service ssh.socket >/dev/null 2>&1 || true
systemctl enable open-vm-tools >/dev/null 2>&1 || true

log "cloud-init: VMware guestinfo datasource for the per-host IP"
printf 'datasource_list: [ VMware, NoCloud, None ]\n' > /etc/cloud/cloud.cfg.d/90-wf-runner-datasource.cfg

cat > /etc/wf-runner.json <<EOF
{"python": "$(/opt/wfpy/bin/python3 -V | cut -d' ' -f2)",
 "pytest": "$(/opt/wfpy/bin/python3 -m pytest --version 2>&1 | awk '{print $2}')",
 "grader_image": "$GRADER_IMAGE", "grader_image_id": "$(docker image inspect -f '{{.Id}}' "$GRADER_IMAGE")",
 "constraints_sha256": "$(sha256sum /opt/wfpy/constraints.txt | cut -d' ' -f1)",
 "pwsh": "$(pwsh -NoProfile -Command '$PSVersionTable.PSVersion.ToString()')", "pester": "$pester",
 "shellcheck": "$(shellcheck --version | awk '/^version:/ {print $2}')", "ripgrep": "$(rg --version | head -1 | awk '{print $2}')"}
EOF
cat /etc/wf-runner.json

if [[ "${WF_RUNNER_TEMPLATE:-1}" == "1" ]]; then
  log "template cleanup: cloud-init state and machine-id (each runner gets its own on first boot)"
  cloud-init clean --logs >/dev/null 2>&1 || true
  truncate -s 0 /etc/machine-id
  rm -f /var/lib/dbus/machine-id
fi
