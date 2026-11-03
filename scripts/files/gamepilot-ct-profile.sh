# Interactive shells on CT 160 use the allowlisting proxy too; the CT firewall rejects anything else.
# Login shells (su -, bash -l) read this from /etc/profile.d. `pct enter` / `pct exec` start a NON-login shell with
# PATH=/sbin:/bin:/usr/sbin:/usr/bin, so 81 provision also sources this file from /etc/bash.bashrc.
# systemd units get the same values from system.conf.d/gamepilot-proxy.conf, sudo sessions from /etc/environment.
case ":$PATH:" in *:/usr/local/bin:*) ;; *) PATH=/usr/local/sbin:/usr/local/bin:$PATH; export PATH ;; esac
export https_proxy=http://192.168.6.155:3129 HTTPS_PROXY=http://192.168.6.155:3129
export http_proxy=http://192.168.6.155:3129 HTTP_PROXY=http://192.168.6.155:3129
export no_proxy=localhost,127.0.0.1 NO_PROXY=localhost,127.0.0.1
export DISABLE_AUTOUPDATER=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 ENABLE_CLAUDEAI_MCP_SERVERS=false
export DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
