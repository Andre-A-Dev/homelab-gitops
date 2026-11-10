# shared/workstations

Files for every workstation (AstraeusNX, Proteus). A file tied to one
machine's hardware stays in its node folder (`astraeus_nx/dotfiles/`); files
for the servers, or for servers and workstations alike, live directly in
`shared/`.

| Path | Contents |
|---|---|
| [`install.sh`](install.sh) | Symlinks the dotfiles below plus the host's own into place (private, not in the public mirror) |
| [`dotfiles/`](dotfiles/README.md) | fish config, SSH client config, session-wide `ssh-agent`, Claude Code user config, autostart entries |
| [`plasmoids/local.homelab.roomclimate/`](plasmoids/local.homelab.roomclimate/README.md) | Room Climate panel widget (Netatmo values from Prometheus) |
| [`scripts/health_checks/`](scripts/health_checks/README.md) | Hardware health checks; `disk_health.py` runs on every workstation, AIO and GPU checks need the AstraeusNX hardware |
| `scripts/obsidian_export.py` | Export the Obsidian vault flat for Claude project knowledge; not linked, run with `python3` (private) |
| `systemd/disk-health.{service,timer}` | Weekly SMART snapshot via `/usr/local/bin/disk-health`, see the health checks README (private) |

## install.sh

Links the workstation dotfiles from the repo checkout into `$HOME`. A file
for every workstation is listed in `COMMON_LINKS` (Room Climate plasmoid,
fish, SSH config, `ssh-agent`, Claude Code, autostart of Syncthing Tray and
ntfyDesktop); a host's own configs (OpenRGB, CoolerControl,
KDE, EasyEffects on AstraeusNX) live in `<host>/dotfiles/` and are added by
the `case` on the hostname. An unknown hostname aborts: add the host there
first.

It also links two repo commands into `~/.local/bin`: `sync-public`
(`scripts/sync_public.py`) and `audit-repo` (`scripts/audit_repo.py`).
CachyOS's fish config puts `~/.local/bin` on the `PATH` only if it exists, so
they work in shells started after the first run. In fish, the abbreviation
`sync-public` (in `dotfiles/fish/config.fish`) expands to
`sync-public ~/Projects/Github/homelab-gitops`, the mirror clone.

```fish
shared/workstations/install.sh --dry-run   # show what would be linked or backed up
shared/workstations/install.sh
```

Idempotent. An existing file or a stale link at a target is moved to
`~/.dotfiles-backup/<timestamp>/` before the link is created. Re-run it after
a pull that moves a linked file; until then `~/.ssh/config` may point
nowhere, so the SSH aliases (`git.home`, `<host>-ro`) are missing.

At the end it checks the tools for working on the repo and prints a
`paru -S --needed ...` line for whatever is missing, required and optional
separately. It never installs anything. Required: `REQUIRED_TOOLS` and
`REQUIRED_PY_MODULES` (CI checks, `shellcheck`, `kubectl`, `flux`, the
`-ro` SSH path); optional: `OPTIONAL_TOOLS` (`actionlint`, `kubeconform`).
A missing tool does not change the exit code. `kubectl` still needs the
`claude-ro` kubeconfig from RUNBOOK "Set Up Claude Code Read-Only SSH"; it
holds a token and is never linked from the repo.

KDE and EasyEffects write their settings through the links into the repo, so
a change in their GUI shows up in `git diff`. That is why KDE configs stay in
the host folder: a shared `kwinrc` would be rewritten by both workstations.
