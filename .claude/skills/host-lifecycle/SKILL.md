---
name: host-lifecycle
description: Add a new host or workstation to the homelab, retire one, or audit how completely a host is registered and set up. Derives the registration points from the repo instead of a fixed checklist; checks hosts read-only.
argument-hint: "add <host> like <reference> | retire <host> | audit <host>"
disable-model-invocation: true
allowed-tools: Bash(python3 .claude/skills/host-lifecycle/scripts/host_refs.py *) Bash(ssh mnemosyne-ro *) Bash(ssh boreas-ro *) Bash(ssh hephaestus-ro *) Bash(ssh zephyros-ro *) Bash(ssh daidalos-ro *) Bash(git status *) Bash(git diff *) Bash(git log *) Bash(git show *)
---

A host appears in many places: fixed host lists in scripts, Prometheus
targets, the rack display, SSH config, read-only keys, docs, the mirror's
sanitizer rules. Nobody keeps that list in their head, and a fixed checklist
goes stale. So the list comes from the repo itself: where does a comparable
host appear, and where is the new one missing.

## Ground rules

- Hosts: read-only via the `-ro` aliases (`CLAUDE.local.md`). Everything on
  a host is a step for the user (bash on servers, fish on workstations).
- Repo: this touches many files across stacks. Show the plan first, edit only
  after the user agreed, and show the diff before proposing commits
  (CLAUDE.md, "Deployment"). Changes to `scripts/sync_public.py`,
  `scripts/export_public.py` or `.publicignore`: flag them every time.
- Public files carry no IPs, internal domains or usernames. IPs go into the
  private files only (`shared/workstations/dotfiles/ssh_config`, `shared/ssh/claude-ro.keys`,
  `CLAUDE.local.md`) or into files the sanitizer rewrites. `REPLACEMENTS` in
  `scripts/sync_public.py` has catch-alls for the LAN range and Tailscale
  IPs, so a new IP does not leak; a dedicated rule only gives it a distinct
  placeholder. Run `/mirror-review` at the next sync either way.
- Static IPs are reserved in the router, never set on the device.
- Vault (`CLAUDE.local.md`): read the setup notes you need, name the notes
  that become stale or are missing, never edit them.
- Never invent setup steps. If neither repo nor vault documents a step (e.g.
  how the node exporter runs on a CachyOS workstation), ask the user and
  name it as a documentation gap.

## The tool

```
python3 .claude/skills/host-lifecycle/scripts/host_refs.py ips  <host>
python3 .claude/skills/host-lifecycle/scripts/host_refs.py refs <host> [--all]
python3 .claude/skills/host-lifecycle/scripts/host_refs.py gaps <reference> <new> [--all]
```

Hits are candidates. Judge every one: a hit can be unrelated (a mouse model
named Proteus), a gap can be intended (the reference's own folder, its
device-specific services). `--alias` for other spellings (`AstraeusNX`:
`--alias astraeus_nx`; note that plain `astraeus` also matches the Windows
boot).

## Roles and references

| Role | Reference | What the role brings (verify against the gaps output) |
|---|---|---|
| Pi server | `hephaestus` (smallest) or `boreas` | repo checkout via a read-only Gitea deploy key (never an account key), webhook handler + 15-min `gitea-pull` cron, apticron symlink, `system-update` + `/etc/system-update.conf`, node exporter, `setup-claude-ro.sh`, `10-no-password.conf`, emergency key, place in `update-hosts.sh` order |
| Kubernetes node | `daidalos` | `daidalos/scripts/bootstrap.sh`, `daidalos/README.md`, `.claude/rules/kubernetes.md` |
| CachyOS workstation | `Proteus` | RUNBOOK "Set Up Claude Code Read-Only SSH" (workstation part), Caddy root CA, claude-ro kubeconfig, node exporter target with `ephemeral: "true"`, rack display, dashboard |
| Pi-based workstation | `Proteus` | like a workstation; monitoring is optional and set up by hand |

The planned machines: Framework 12 (CachyOS workstation, own folder created
by the user later, replaces Proteus) and the ClockworkPi uConsole
(workstation on Raspberry Pi hardware, arm64: check tool support; light
monitoring optional, not automated).

Shared workstation parts live under `shared/workstations/` (`dotfiles/`,
`plasmoids/`, `systemd/`, `scripts/`). A new workstation needs a branch in
the hostname `case` of `shared/workstations/install.sh`.

## A. add `<host>` like `<reference>`

1. **Clarify** what the arguments don't say: role, OS and architecture, LAN
   and/or Tailscale, which optional parts (monitoring, rack display, Pi-hole
   DNS name). The host name follows the existing naming (Greek mythology);
   the user picks it.
2. **Gaps:** `host_refs.py gaps <reference> <host>`. Sort every file into:
   - **needed:** fixed host lists, targets, docs tables
   - **optional:** the user decides (dashboard rows, rack display)
   - **not applicable:** the reference's own folder and device-specific
     parts. Say why for each.
3. **Host steps, in this order**, each taken from its source (cite it):
   - before the device is online: router IP reservation, Tailscale
   - Pi server: vault `1_Einrichtung_Pi.md`, `2_Pull_Deploy_Pis.md`, then
     apticron, `system-update`, node exporter, `setup-claude-ro.sh`,
     emergency key line; `10-no-password.conf` only after key login has
     proven stable across reboots (RUNBOOK "Disable SSH Password Login")
   - Kubernetes node: `daidalos/scripts/bootstrap.sh` and its README
   - workstation: RUNBOOK "Set Up Claude Code Read-Only SSH" (on the
     workstation), "Import Caddy Root Certificate on a New Device" (plus the
     Zephyros root for mobile devices), the claude-ro kubeconfig; the new
     key in `claude-ro.keys` means `setup-claude-ro.sh` on **every** host
     after the push
   - workstation, not in the repo: `.claude/settings.local.json` (vault
     Read denies, `additionalDirectories`), the vault path in
     `CLAUDE.local.md`, Gitea SSH key
4. **Outside the repo:** DNS (Pi-hole local records, deSEC), Gitea webhook
   (Pi servers), Healthchecks pings (servers), new vault folder per
   `CLAUDE.local.md`.
5. **Order of repo changes:** registrations that alert (Prometheus targets
   of servers, blackbox probes) go in only once the host serves them;
   workstation targets carry `ephemeral: "true"` and are exempt from
   availability alerts (`alerts.yml`, "System Alerts").
6. After agreement: prepare the diff, run the CI checks, then `/repo-review`.
   After the user's push: **C. audit**.

## B. retire `<host>`

1. **Inventory:** `host_refs.py refs <host> --all`. Each hit: remove,
   rewrite (replacement host) or keep (history, decisions in
   `ARCHITECTURE.md`). Grafana dashboards are changed in the Grafana UI and
   exported (RUNBOOK "Export Grafana Dashboards"), not edited by hand.
2. **Access first**, before anything cosmetic. A retired machine that still
   holds keys is the actual risk.
   - Its line in `shared/ssh/claude-ro.keys` → push → `setup-claude-ro.sh`
     on every host. Verify per host:
     `ssh <host>-ro 'grep -c "claude-ro@<retired>" ~/.ssh/authorized_keys'`
     must print `0`.
   - Its admin key lines in `~/.ssh/authorized_keys` of `<user>` on every
     host, by key comment (pattern as in RUNBOOK "Emergency SSH Key"). Not
     checkable with `claude-ro`: the user verifies.
   - Gitea and GitHub SSH keys and tokens of that machine (servers: the
     deploy key on `homelab-infra`; workstations: the account key); the Tailscale
     device (frees its IP, so no later device inherits a `from=` match).
   - The claude-ro kubeconfig token is shared by all workstations: rotate it
     (RUNBOOK "Kubernetes (Daidalos)") if the device leaves the household
     unwiped.
3. **Monitoring:** remove the target and deploy before the device goes
   dark for good if it is a server (CLAUDE.md, "Decommissioning a probed
   service"); a workstation target is `ephemeral` and can go in one step.
4. **Docs:** nodes table in `CLAUDE.md`, `README.md`, `wiki/Home.md`,
   `CLAUDE.local.md` (vault path), vault notes to archive (name them).
5. **Mirror:** sanitizer rules for the retired IPs may stay; flag either way.

## C. audit `<host>`

Static: `host_refs.py gaps <peer> <host>` and the reverse, with a peer of the
same role. Report only gaps the role requires.

Prometheus (all hosts; `instance` is the host name as written in
`prometheus.yml`):

- `up{job="node-exporter",instance="<instance>"}`
- servers: `system_update_exit_code`, `system_update_last_run_timestamp_seconds`
  (age), `system_update_failed_units_total`, `system_update_reboot_required`

Servers via `<host>-ro`, one command:

- `hostnamectl hostname`, `systemctl --failed --no-legend`,
  `timedatectl show -p NTPSynchronized`
- `ls -l /etc/apticron/`: `apticron.conf` a symlink into the repo,
  `apticron.local.conf` present. Use `ls -l`/`readlink`, never `readlink -f`
  (claude-ro cannot enter `/home/<user>`, `-f` prints nothing)
- `ls -l /usr/local/bin`: tools are symlinks into the repo, not copies
- `ls -l /etc/system-update.conf`, `cat /etc/ssh/sshd_config.d/10-no-password.conf`
- `id claude-ro` (groups `adm`, `systemd-journal`), `sudo -n -l` (only
  `docker ps`, `docker logs`)
- `tail -n 20 /var/log/webhook-handler.log`,
  `journalctl -t gitea-pull --since -7d` (repeated errors only)

Workstations have no `-ro` alias. Prometheus only, plus local checks when the
audit runs on that workstation itself (`hostnamectl hostname`): `~/.ssh/config`
a symlink into the repo, `$SSH_AUTH_SOCK` set, `~/.ssh/id_ed25519_claude_ro.pub`
listed in `claude-ro.keys`, `kubectl config current-context` is
`claude-ro@daidalos`, `.claude/settings.local.json` present.

## Report

German, informal "du"; identifiers, paths and commands in English. For add
and retire:

```
## <Add|Retire> <host> (<role>, Referenz <reference>)

REPO (nach deiner Freigabe)
- <file> — <add|remove|rewrite>: <what> [MIRROR-TOOLING]

AUF DEM HOST / DEN HOSTS (Reihenfolge)
- <step> — Quelle: <RUNBOOK section | vault note | script>

AUSSERHALB
- <router, DNS, Tailscale, Gitea, GitHub, Healthchecks>

DOKU-LÜCKEN
- <step nobody documented, vault notes to create or archive>

NICHT GEPRÜFT
- <what and why>
```

For audit: BEFUNDE (missing or broken, with evidence), OK (one line per
check group), GEPRÜFT (command → output), NICHT GEPRÜFT.
