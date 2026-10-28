# CLAUDE.md — homelab-infra

GitOps source of truth for a self-hosted homelab. This repo deploys to live
systems. When unsure whether something is safe to touch, stop and ask.

Internal context (IPs, hostnames, paths, knowledge base) lives in
`CLAUDE.local.md` next to this file: versioned in the private repo, excluded
from the public mirror. This file is public: keep it free of IPs, internal
domains, usernames and secrets. `CLAUDE.local.md`, `.gitea/` and the mirror
tooling in `scripts/` exist only in the private repo.

## Nodes

| Node                         | Hardware                                                   | Role                                     |
|------------------------------|------------------------------------------------------------|------------------------------------------|
| Mnemosyne                    | Raspberry Pi 5                                             | Primary server, almost all stacks        |
| Boreas                       | Pi 3                                                       | Local DNS server & Ad Blocker            |
| Zephyros                     | Pi 3                                                       | Remote DNS server & Ad Blocker           |
| Hephaestus                   | Pi 3                                                       | Viessmann heating integration            |
| AstraeusNX                   | x86_64 workstation                                         | Development machine, x86-only tooling    |
| Daidalos                     | Dell OptiPlex 7070 Micro, i5-9500T (6C), 16 GB, 256 GB SSD | First Kubernetes node (learning cluster) |
| Proteus                      | Acer Switch Alpha 12 (N16P3)                               | Secondary workstation, mobile, CachyOS   |
| TBA                          | Framework 12 pro                                           | Planned replacement for Proteus; Ordered |
| TBA                          | ClockworkPi uConsole                                       | Planned Cyberdeck; Ordered               |

Edits happen on the workstations: AstraeusNX (primary), Proteus and the
planned Framework 12. Never edit versioned files directly on a server.

Devices (not deploy targets, but monitored and controlled from the stacks):

| Device    | Hardware                        | Integration                                                                       |
|-----------|---------------------------------|-----------------------------------------------------------------------------------|
| Pygmalion | Prusa MK4S + Buddy3D camera     | Galatea (control platform on Mnemosyne), PrusaLink, Shelly plug + climate sensors |

**Physical devices are off-limits for write actions.** Never send commands to
PrusaLink, Shelly plugs or any other device API (start/stop/pause, power
on/off, settings) — not even for testing. Cutting power mid-print ruins the
print. Read-only `GET`s for verification are fine.

## Guiding principles

- **No complexity without concrete benefit.** No new abstractions, dependencies
  or tooling without a named reason. Prefer the boring, auditable, CLI-first
  solution. If a simpler approach exists, propose it instead.
- **Silence is not success.** No error output proves nothing. Verify with real
  output. Never hide failures (`2>/dev/null`, `|| true`) without a stated reason.
- **Empirical identifiers.** Never assume container names, metric names, PromQL
  labels, file paths or device IDs. Look them up first (`docker ps`,
  `curl <exporter>/metrics`, `ls`), then write them into code or dashboards.
- **Surgical changes.** Every changed line must trace back to the request.
  Don't reformat, reorder or "improve" adjacent code, comments or config.
  Mention unrelated dead code or issues, don't fix them unasked. Remove only
  what your own change made unused. Reason: the deploy plan is file-based
  (`git diff --name-only`), so a whitespace-only edit to a mounted config
  recreates its container, and a drive-by fix in another stack turns the
  change into a multi-stack one.

## Repository layout

- `mnemosyne/stacks/<stack>/` — one Compose stack per service
  (`docker-compose.yml`, `.env.example`)
- `mnemosyne/scripts/` — Mnemosyne scripts (backup, verification, exporters)
- `mnemosyne/systemd/` — all Mnemosyne systemd units, grouped by topic
  (`backup/`, `restic/`); drop-ins in `<unit>.service.d/` next to their unit.
  Never put units inside stack folders.
- `mnemosyne/docker/` — Docker daemon config (`daemon.json`), deployed by hand
  to `/etc/docker/` (see `mnemosyne/README.md`)
- `mnemosyne/webhook/` — deploy webhook configuration
- `<node>/` — per-node config and scripts (e.g. `hephaestus/`)
- `astraeus_nx/` — AstraeusNX on CachyOS, the primary OS of the workstation;
  `astraeus/` — the Windows boot of the same machine (separate SSD)
- `clusters/daidalos/` — Kubernetes manifests, reconciled by Flux
- `shared/scripts/` — multi-host scripts, symlinked to `/usr/local/bin/`,
  hostname-based dispatch
- `shared/compose-profiles.yml` — profile vocabulary and per-host activation
- `shared/apticron/apticron.conf` — symlinked to `/etc/apticron/apticron.conf`
  on all Pis; recipients stay host-local in `/etc/apticron/apticron.local.conf`
- `.gitea/workflows/` — CI (validation, env drift, linting)
- `.gitea/scripts/` — the CI check scripts (`check_configs.py`,
  `check_env_drift.py`), runnable locally
- `scripts/` — repo tooling, including the public-mirror tooling; paths kept
  out of the mirror are listed in `scripts/config/.publicignore`
- `wiki/` (synced to the Gitea wiki on push), `RUNBOOK.md`, `SERVICES.md`,
  `ARCHITECTURE.md` — English repo docs, see "Documentation"
- `.env` — Never read .env. Always use the .env.example as reference.

## Language

- Code, comments, dashboards, metric and log names, this file: **English**.
- Commit messages (proposals only, see "Git"): Conventional Commits. Subject in
  **English imperative**, body in **English** explaining *why*:

      feat(caddy): Add a health check route

      Blackbox exporter needs an unauthenticated endpoint to probe.

- German prose documentation lives in the Obsidian vault, not in this repo.
- No Co-Authored-By

## Documentation

- Repo docs: a new service, URL or port → `SERVICES.md`; a new operational
  procedure → `RUNBOOK.md`; a design decision → `ARCHITECTURE.md`; topic
  details → `wiki/`. Update the affected file in the same change set.
- German docs live in the Obsidian vault (path and folder map in
  `CLAUDE.local.md`). New notes go into the folder of the host and topic they
  describe. Edit the vault only when I ask; otherwise name the notes a change
  makes stale.
- No real usernames in tracked files: write `<user>` for the login user and
  `/home/<user>` for its home. Code still has hardcoded ones (open point);
  don't add new ones.

## Secrets

- `.env` — plaintext, gitignored, lives only on the target host. Never read,
  print, edit or commit.
- Encrypted secrets in the repo (SOPS + age) are planned, **not implemented**.
  Don't create `.env.sops` files, don't assume they exist, don't introduce SOPS
  as a side effect of another change.
- New secret needed: add the key with an empty value to `.env.example` and tell
  me the key and why.
- Values containing `$` (bcrypt hashes etc.) must be written as `$$`: Compose
  interpolates `env_file` values too. Check with
  `docker exec <container> printenv VAR | wc -c` (bcrypt = 61).

## Git — you never commit

I commit and push myself, after reviewing the code and the diff.

- Never run `git commit`, `git push`, `git add`, `git stash`, `git reset` or
  anything else that changes the index or history. Leave all changes unstaged
  in the working tree so `git diff` shows the complete change.
- Read-only git is fine: `git status`, `git diff`, `git log`, `git show`.
- At the end of each change set, propose the commit(s):
  - the commit message in a code block (format: see "Language")
  - the files belonging to each commit, if the change should be split
  - the manual step needed after push, if any (see "Deployment")
  - what you verified and what is still unverified (see "Verification")

## Deployment — not your job

Current flow:

- **Mnemosyne:** push → Actions job `validate` → job `deploy` (`needs: validate`)
  calls the host webhook synchronously → `mnemosyne/scripts/deploy-handler.sh`
  fast-forwards to exactly the validated SHA and applies a per-file plan
  (`deploy_plan.py`):
  - `docker-compose.yml` changed → `docker compose up -d` for that stack
  - bind-mounted config changed (`alerts.yml`, Caddyfile, Alertmanager
    template, ...) → `--force-recreate` of exactly the services mounting it
  - build context changed → `docker compose build` + recreate
  - then verifies the touched containers; a failure turns the job red (ntfy).
- **Never applied, reported as `manual=`:** `gitea`, `gitea-runner` (recreating
  them kills the running job), changed systemd units, and changed files that no
  running service mounts (`<stack>:unmapped`). Ignored: `*.md`, `.env.example`,
  Grafana dashboards.
- **Boreas, Hephaestus, Zephyros:** Gitea repo webhook plus a 15-minute cron
  `git pull`. Zephyros applies its `caddy/` (validated first) and `monitoring/`
  stacks. Boreas and Hephaestus only restart specific systemd services; any
  other change there needs a manual step.
- **Not deployed by anything:** `/etc/docker/daemon.json`, systemd unit files,
  `.env`.

Consequences:

- Check what the handler did instead of assuming: the `deploy` job log or
  `journalctl -u webhook | grep result=` on Mnemosyne (`applied=`, `manual=`,
  `failed=`). State the manual step for every `manual=` entry and every host
  the handlers do not cover; don't ask for a recreate the handler already did.
- Your job ends at a verified, uncommitted change plus a commit proposal. Never
  run `docker compose up/down/restart` against production unless I ask in this
  session.
- Changes touching more than one stack: show me the diff first.
- Never modify the public-mirror tooling (`export_public.py`, `sync_public.py`,
  `audit_repo.py`) without flagging it — it gates what becomes public.

## Kubernetes (Daidalos)

Rules for `clusters/daidalos/` live in `.claude/rules/kubernetes.md`.
Short version: Flux reconciles that directory, so a push there is a deploy.

## Compose profiles

Containers are deactivated **only** via profiles. The vocabulary is fixed in
`shared/compose-profiles.yml` (`inactive`, `seasonal`, `cli`, `debug`) and
enforced by CI. Don't comment out services, don't invent new profiles.

## Update classes

Every service carries `labels: homelab.update-class: "A" | "B" | "C"`, enforced
by CI (`check_configs.py`). A = stateful with schema migrations (major/minor
pin, backup before upgrading), B = stateless infra and exporters (major tag),
C = exactly the services with `build:`. A new service needs its label in the
same commit. Details: `wiki/Update-Strategy.md`.

Diun labels, also CI-enforced: class C carries `diun.enable: "false"`. Class A
applications on Mnemosyne (not databases) carry `diun.watch_repo: "true"` and
`diun.include_tags` matching the pinned tag and newer only (`$$` for `$`).
When bumping such a tag, move the regex lower bound in the same commit.

## Verification — do it yourself, before you report

Verify on your own initiative. Don't ask whether you should, don't hand me
checks you can run yourself. "Done" means verified, not written.

**Before proposing a commit (static, local):**

- CI checks, always: `python3 .gitea/scripts/check_configs.py` (compose,
  alert rules, dashboards) and `python3 .gitea/scripts/check_env_drift.py`
- Compose: `docker compose -f <file> config --quiet`
- Shell: `shellcheck <script>`; run it with `--help` or a dry-run flag if it
  has one
- Python: run the module or its tests
- JSON/YAML (dashboards, Prometheus config): parse it, e.g.
  `python3 -m json.tool <file>`

**Against the live system (read-only, details in `CLAUDE.local.md`):**

- Every metric name, label and container name you write must exist. Look it up
  first.
- Every PromQL expression for a dashboard or alert: run it against the
  Prometheus HTTP API first. Check that it returns data, the expected labels
  and plausible values. An empty result is a finding, not a success.
- Exporter changes: `curl` the current `/metrics` output to see what exists
  today.

**After I deployed:** when I say a change is live, check the effect
read-only (container state, logs since restart, target `up`, the new metric or
query returning data) and report the actual output.

**Reporting:** for every check, state the command and the relevant output.
List explicitly what could not be verified and why.

## Known gotchas

- **Inode swap:** git and editors replace files atomically (new inode).
  Containers with bind-mounted config (Prometheus, Caddy) need
  `--force-recreate`; `/-/reload` or `caddy reload` is not enough. The
  Mnemosyne deploy handler does this per changed file; it matters for manual
  changes and for Boreas/Hephaestus.
- **Caddy:** after Caddyfile changes the container must be recreated
  (`up -d --force-recreate` or `down && up -d`), never `restart` or
  `caddy reload`. The Mnemosyne deploy handler recreates it on its own.
- **up vs. restart:** env/config changes need `docker compose up -d`;
  add `--build` when `Dockerfile` or `app.py` changed.
- **Networks:** `caddy_proxy` is external and pre-created manually. Stack-owned
  networks must never be pre-created. Start order matters.
- **arm64 gaps:** check image architecture support before proposing an image for
  a Pi node. x86-only tools belong on AstraeusNX.
- **`.env.example` drift:** every key used in a compose file must exist in
  `.env.example`. CI enforces this.
- **Executable bit:** new scripts need `chmod +x`. Mention it in the commit
  proposal so I can confirm the mode in the diff.
- **Static IPs** are assigned centrally in the router, never on the device.
  Storage mounts use UUID-based fstab entries.
- **System updates** are rolled out manually (`shared/scripts/system-update.sh`),
  never on a timer. Don't propose unattended upgrades.

## Tech stack

- Docker Compose for production; Kubernetes + Flux only on Daidalos (learning)
- Caddy with internal CA, external `caddy_proxy` network
- Python: standard library first; Flask, `prometheus_client`, `requests`
- Prometheus + Grafana, Node Exporter, cAdvisor, textfile-collector pattern
- Backups: restic, systemd timers

## Working with me

- Be direct. If my request is over-engineered or there's a simpler path, say so
  with the trade-off.
- If my request is ambiguous, name the ambiguity and ask. Don't silently pick an
  interpretation.
- Verify each step with real output before moving on.
