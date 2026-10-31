# Interactive shells on CT 160 (pct enter, su -) use the allowlisting proxy too; the CT firewall rejects anything else.
# systemd units get the same values from system.conf.d/gamepilot-proxy.conf, sudo sessions from /etc/environment.
export https_proxy=http://192.168.6.155:3129 HTTPS_PROXY=http://192.168.6.155:3129
export http_proxy=http://192.168.6.155:3129 HTTP_PROXY=http://192.168.6.155:3129
export no_proxy=localhost,127.0.0.1 NO_PROXY=localhost,127.0.0.1
export DISABLE_AUTOUPDATER=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 ENABLE_CLAUDEAI_MCP_SERVERS=false
