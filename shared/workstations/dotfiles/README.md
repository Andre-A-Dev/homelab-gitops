## What is tracked here

All files are linked into place by [`../install.sh`](../README.md#installsh).

| Path | Purpose |
|---|---|
| [`ssh_config`](ssh_config) | SSH client config covering all homelab hosts (private, not in the public mirror) |
| [`environment.d/ssh-agent.conf`](environment.d/ssh-agent.conf) | Session-wide `ssh-agent` socket + ksshaskpass, see RUNBOOK "Set Up Claude Code Read-Only SSH" |
| [`claude/CLAUDE.md`](claude/CLAUDE.md) | Claude Code user instructions for all projects, linked to `~/.claude/CLAUDE.md` |
| [`claude/settings.json`](claude/settings.json) | Claude Code user settings, linked to `~/.claude/settings.json` |

## SSH config

Linked to `~/.ssh/config`.

Covers:
- `mnemosyne` / `boreas` / `hephaestus` / `daidalos` — LAN hosts by IP
- `git.home` — Gitea SSH on port 2222 via Mnemosyne's Tailscale IP (the remote
  LAN shares the subnet, so the LAN IP is unreachable there)
- `mnemosyne-ts` / `boreas-ts` / `hephaestus-ts` / `daidalos-ts` — Tailscale FQDN entries
- `zephyros` — Tailscale IP (remote node, no LAN access)
- `<host>-ro` — read-only user `claude-ro` for Claude Code, via Tailscale IP

## Claude Code

Only the user-level config is linked. Everything else in `~/.claude/` stays
local: `.credentials.json` holds the login token, and the session transcripts
(`projects/*/*.jsonl`) are keyed by the absolute repo path and full of tool
output.

Claude Code writes `settings.json` itself (`/config`, `/model`, effort), so
such changes show up in `git diff` like the KDE ones.

The auto-memory (`~/.claude/projects/<encoded-repo-path>/memory/`) is synced
between the workstations by Syncthing, not by this repo: it holds private
notes that must not reach the public mirror. Both workstations need the repo
at the same absolute path, or the encoded folder names differ.
