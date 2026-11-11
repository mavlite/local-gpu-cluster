# ARTEX toolbox layer — adds a fixed pentest toolset on top of the running ARTEX image.
#
# Why: the ARTEX image is python:3.12-slim + nmap/curl/Playwright only, while upstream's
# design assumes "Kali tooling". Behind the egress lock the agents cannot install anything,
# so on 2026-11-10/11 ~1/3 of task 3's tokens went into hand-writing SSH crypto. Tools are
# baked in here instead of opening the lock to package mirrors.
#
# Built OUTSIDE the lock (a temporary build CT with internet, see build.sh), then moved into
# LXC 158 with docker save/load. Release binaries are sha256-pinned; git-sourced tools are
# pinned to a commit SHA and the build FAILS if the clone resolves to anything else. apt and
# pip resolve at build time and are recorded in the lock files build.sh writes.
#
# No "# syntax=" line on purpose: it makes BuildKit contact Docker Hub.
ARG BASE=artex:local
FROM ${BASE}

# --- release binaries (sha256 of the published asset) --------------------------------
ARG FFUF_VERSION=2.3.0
ARG FFUF_SHA256=b2a3c725fcb9da175159682f54d6e9149f2905b00d84d155e7efc5d599975ceb
ARG NUCLEI_VERSION=3.11.1
ARG NUCLEI_SHA256=ea63d4ae232808cd7c6bc00d0142428e231fab59dae01042246097d195835ab6
ARG NUCLEI_TEMPLATES_VERSION=10.5.1
ARG NUCLEI_TEMPLATES_SHA256=30fe1cfaced4635f886e4a2673641bba86998dceec28e468274524e789aa56eb
ARG HTTPX_VERSION=1.12.0
ARG HTTPX_SHA256=9d8439e8b6c9aa7d1e2314817a392e00d5178da3af5652f7475f88868f418f76
ARG KATANA_VERSION=1.8.0
ARG KATANA_SHA256=cb027ae99d0757061b70c07a15c7a80f62c5a535ab52c4a329f80c4b0a7309d2
ARG DALFOX_VERSION=3.3.0
ARG DALFOX_SHA256=7b6deb400cda5a100b7a8978e46ef974133df61e9ad1df5ed014c37093037a1c
ARG FEROXBUSTER_VERSION=2.13.1
ARG FEROXBUSTER_SHA256=7985c00e6803b0f25d5e9139f7472279f3f4d891429627a5cedc629e53992d80
# --- git-sourced tools (commit SHA is the integrity anchor) ---------------------------
ARG SECLISTS_TAG=2026.2
ARG NIKTO_TAG=2.6.1
ARG NIKTO_SHA=d201dac320fc5187eac75e723dd07a716196ec5a
ARG GRAPHW00F_SHA=4901f824140a2da168876412593c213afbdd75fb
ARG ENUM4LINUX_NG_SHA=288826fd9ee89b61d846e59f17b77bae4fb671ee
# netexec: the v1.5.1 tag is uninstallable (its lock pins "certipy-ad (unavailable)");
# main fixed that to certipy-nxc, so pin to a main commit instead of the release tag.
ARG NETEXEC_REF=917fcebc3bb97085dcc84555aa277cb7ea05f9f1
ARG EXPLOITDB_TAG=2026-10-27
ARG EXPLOITDB_SHA=f08907b131f851c2ec3c052906f82ca83703ebef
# --- optional heavyweight ------------------------------------------------------------
ARG WITH_METASPLOIT=1

# System tools (Debian 12 bookworm). Grouped by purpose, one layer.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      openssh-client sshpass hydra ncat socat procps xxd sqlmap testssl.sh \
      masscan snmp onesixtyone smbclient \
      default-mysql-client postgresql-client redis-tools \
      git file binutils iproute2 iputils-ping traceroute zip unzip dnsutils \
      perl libnet-ssleay-perl libwhisker2-perl libxml-writer-perl \
      ca-certificates gnupg \
      build-essential python3-dev libffi-dev \
 && rm -rf /var/lib/apt/lists/*

# Python LIBRARIES: installed into the shared interpreter because agents `import` them
# in the scripts they write. Keep this list import-only — CLI tools go through pipx below
# so a heavy dependency tree can never break what the agents import.
COPY requirements.txt /tmp/toolbox-requirements.txt
RUN PIP_ROOT_USER_ACTION=ignore pip install --no-cache-dir --retries 5 --timeout 30 \
      -r /tmp/toolbox-requirements.txt \
 && rm /tmp/toolbox-requirements.txt

# Python CLI tools, each isolated in its own venv by pipx, binaries linked into /usr/local/bin.
ENV PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin
RUN PIP_ROOT_USER_ACTION=ignore pip install --no-cache-dir pipx \
 && for p in git-dumper clairvoyance arjun wafw00f; do pipx install --quiet "$p"; done \
 && pipx install --quiet "git+https://github.com/Pennyw0rth/NetExec@${NETEXEC_REF}" \
 && pipx list --short

# Release binaries: every download sha256-verified against the published checksum.
# Each spec is "localfile|url|sha256"; the loop fails the build on any mismatch.
RUN set -eu; cd /tmp; \
    for spec in \
      "ffuf.tgz|https://github.com/ffuf/ffuf/releases/download/v${FFUF_VERSION}/ffuf_${FFUF_VERSION}_linux_amd64.tar.gz|${FFUF_SHA256}" \
      "nuclei.zip|https://github.com/projectdiscovery/nuclei/releases/download/v${NUCLEI_VERSION}/nuclei_${NUCLEI_VERSION}_linux_amd64.zip|${NUCLEI_SHA256}" \
      "httpx.zip|https://github.com/projectdiscovery/httpx/releases/download/v${HTTPX_VERSION}/httpx_${HTTPX_VERSION}_linux_amd64.zip|${HTTPX_SHA256}" \
      "katana.zip|https://github.com/projectdiscovery/katana/releases/download/v${KATANA_VERSION}/katana_${KATANA_VERSION}_linux_amd64.zip|${KATANA_SHA256}" \
      "dalfox.tgz|https://github.com/hahwul/dalfox/releases/download/v${DALFOX_VERSION}/dalfox-v${DALFOX_VERSION}-linux-x86_64.tar.gz|${DALFOX_SHA256}" \
      "ferox.tgz|https://github.com/epi052/feroxbuster/releases/download/v${FEROXBUSTER_VERSION}/x86_64-linux-feroxbuster.tar.gz|${FEROXBUSTER_SHA256}" \
    ; do \
      oldifs=$IFS; IFS='|'; set -- $spec; IFS=$oldifs; \
      curl -fsSLo "$1" "$2"; \
      echo "$3  $1" | sha256sum -c -; \
    done; \
    tar -xzf ffuf.tgz -C /usr/local/bin ffuf; \
    for z in nuclei httpx katana; do unzip -q -o "$z.zip" "$z" -d /usr/local/bin; done; \
    tar -xzf dalfox.tgz -C /usr/local/bin --strip-components=1 "dalfox-v${DALFOX_VERSION}-linux-x86_64/dalfox"; \
    tar -xzf ferox.tgz  -C /usr/local/bin feroxbuster; \
    chmod +x /usr/local/bin/ffuf /usr/local/bin/nuclei /usr/local/bin/httpx /usr/local/bin/katana /usr/local/bin/dalfox /usr/local/bin/feroxbuster; \
    rm -f ./*.tgz ./*.zip
# ProjectDiscovery's httpx binary shadows nothing: the Python httpx library is deliberately
# NOT installed (requirements.txt uses requests/aiohttp) so `httpx` always means the scanner.

# Pinned nuclei-templates release in nuclei's default location; update checks disabled
# (they would only hit the egress lock).
RUN set -eu; cd /tmp \
 && curl -fsSLo nt.tgz "https://github.com/projectdiscovery/nuclei-templates/archive/refs/tags/v${NUCLEI_TEMPLATES_VERSION}.tar.gz" \
 && echo "${NUCLEI_TEMPLATES_SHA256}  nt.tgz" | sha256sum -c - \
 && mkdir -p /root/nuclei-templates \
 && tar -xzf nt.tgz -C /root/nuclei-templates --strip-components=1 \
 && rm -f nt.tgz
COPY nuclei-config.yaml /root/.config/nuclei/config.yaml

# Git-sourced tools, each pinned to a commit SHA: the clone is rejected if HEAD differs.
# Each spec is "url|ref|expected-sha|dest".
RUN set -eu; \
    for spec in \
      "https://github.com/sullo/nikto.git|${NIKTO_TAG}|${NIKTO_SHA}|/opt/nikto" \
      "https://github.com/dolevf/graphw00f.git|main|${GRAPHW00F_SHA}|/opt/graphw00f" \
      "https://github.com/cddmp/enum4linux-ng.git|master|${ENUM4LINUX_NG_SHA}|/opt/enum4linux-ng" \
      "https://gitlab.com/exploit-database/exploitdb.git|${EXPLOITDB_TAG}|${EXPLOITDB_SHA}|/opt/exploitdb" \
    ; do \
      oldifs=$IFS; IFS='|'; set -- $spec; IFS=$oldifs; \
      git clone --quiet --depth 1 --branch "$2" "$1" "$4"; \
      got=$(git -C "$4" rev-parse HEAD); \
      if [ "$got" != "$3" ]; then echo "PIN MISMATCH $1: got $got want $3" >&2; exit 1; fi; \
      rm -rf "$4/.git"; \
    done; \
    printf '#!/bin/sh\nexec perl /opt/nikto/program/nikto.pl "$@"\n'            > /usr/local/bin/nikto; \
    printf '#!/bin/sh\nexec python3 /opt/graphw00f/main.py "$@"\n'              > /usr/local/bin/graphw00f; \
    printf '#!/bin/sh\nexec python3 /opt/enum4linux-ng/enum4linux-ng.py "$@"\n' > /usr/local/bin/enum4linux-ng; \
    chmod +x /usr/local/bin/nikto /usr/local/bin/graphw00f /usr/local/bin/enum4linux-ng; \
    ln -sfn /opt/exploitdb/searchsploit /usr/local/bin/searchsploit; \
    sed -i 's#^path_array=.*#path_array=(/opt/exploitdb)#' /opt/exploitdb/.searchsploit_rc 2>/dev/null || true

# Metasploit Framework from the vendor apt repo (ARG WITH_METASPLOIT=0 to skip).
RUN set -eu; \
    if [ "${WITH_METASPLOIT}" = "1" ]; then \
      install -m 0755 -d /etc/apt/keyrings; \
      curl -fsSL https://apt.metasploit.com/metasploit-framework.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/metasploit.gpg; \
      echo "deb [signed-by=/etc/apt/keyrings/metasploit.gpg] https://apt.metasploit.com/ lucid main" \
        > /etc/apt/sources.list.d/metasploit.list; \
      apt-get update; \
      DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends metasploit-framework; \
      rm -rf /var/lib/apt/lists/*; \
      msfconsole --version; \
    else echo "metasploit skipped (WITH_METASPLOIT=${WITH_METASPLOIT})"; fi

# Debian ships testssl.sh as /usr/bin/testssl; agents (and upstream docs) call it testssl.sh.
RUN ln -sfn /usr/bin/testssl /usr/local/bin/testssl.sh

# SecLists subset at a pinned tag, in the Kali path agents expect; checksums recorded.
COPY seclists-files.txt /tmp/seclists-files.txt
RUN set -eu; mkdir -p /usr/share/seclists; cd /usr/share/seclists \
 && grep -vE '^\s*(#|$)' /tmp/seclists-files.txt | while read -r p; do \
      mkdir -p "$(dirname "$p")"; \
      curl -fsSLo "$p" "https://raw.githubusercontent.com/danielmiessler/SecLists/${SECLISTS_TAG}/$p"; \
    done \
 && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS \
 && echo "SecLists ${SECLISTS_TAG} subset" > VERSION \
 && rm /tmp/seclists-files.txt \
 && ln -sfn /usr/share/seclists /usr/share/wordlists

# Rate-limit wrappers: the targets are live production services, and tool defaults
# (hydra 16 tasks, masscan unlimited) are far more aggressive than a careful operator.
# The real binaries move to /opt/toolbox/real and /usr/local/bin/<tool> becomes the
# clamping shim, so the cap cannot be bypassed by argument wording.
COPY toolbox-ratelimit.sh /opt/toolbox/toolbox-ratelimit
RUN set -eu; \
    chmod +x /opt/toolbox/toolbox-ratelimit; \
    mkdir -p /opt/toolbox/real; \
    for t in hydra masscan ffuf feroxbuster nuclei; do \
      src=$(command -v "$t"); \
      mv "$src" "/opt/toolbox/real/$t"; \
      ln -sfn /opt/toolbox/toolbox-ratelimit "/usr/local/bin/$t"; \
    done; \
    ls -l /opt/toolbox/real/

# Inventory the agents (and we) can read: what is installed, at which version.
COPY toolbox-manifest.sh /usr/local/bin/toolbox-manifest
RUN chmod +x /usr/local/bin/toolbox-manifest && toolbox-manifest > /opt/TOOLBOX.txt && cat /opt/TOOLBOX.txt
