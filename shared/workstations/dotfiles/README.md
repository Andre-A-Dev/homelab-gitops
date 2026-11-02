## What is tracked here

All files are linked into place by [`../install.sh`](../README.md#installsh).

| Path | Purpose |
|---|---|
| [`ssh_config`](ssh_config) | SSH client config covering all homelab hosts (private, not in the public mirror) |
| [`environment.d/ssh-agent.conf`](environment.d/ssh-agent.conf) | Session-wide `ssh-agent` socket + ksshaskpass, see RUNBOOK "Set Up Claude Code Read-Only SSH" |

## SSH config

Linked to `~/.ssh/config`.

Covers:
- `mnemosyne` / `boreas` / `hephaestus` / `daidalos` — LAN hosts by IP
- `git.home` — Gitea SSH on port 2222 via Mnemosyne's Tailscale IP (the remote
  LAN shares the subnet, so the LAN IP is unreachable there)
- `mnemosyne-ts` / `boreas-ts` / `hephaestus-ts` / `daidalos-ts` — Tailscale FQDN entries
- `zephyros` — Tailscale IP (remote node, no LAN access)
- `<host>-ro` — read-only user `claude-ro` for Claude Code, via Tailscale IP
