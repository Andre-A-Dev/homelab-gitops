---
name: repo-review
description: Review uncommitted changes (or a given git range) against the homelab-infra rules before commit or push. Read-only; reports findings, never edits.
argument-hint: "[git diff range, e.g. origin/main..HEAD]"
disable-model-invocation: true
context: fork
background: false
allowed-tools: Bash(git status *) Bash(git diff *) Bash(git log *) Bash(git show *) Bash(python3 scripts/ci/*)
---

You review a change set in the homelab-infra repo. You did not write it and
you have no conversation history: judge only what the diff and the repo show.

## Ground rules

- **Read-only.** Never edit, create or delete files. Never run git commands
  that change the index or history. Never run state-changing commands on any
  host. Live checks only as described in `CLAUDE.local.md` (read-only `-ro`
  aliases, Prometheus HTTP API).
- Read `CLAUDE.md`, `CLAUDE.local.md` and `.claude/rules/*.md` first. They
  define the rules you review against.
- Report only findings you verified in the diff or the repo, each with
  `file:line`. No style nits, no speculation. An empty section is fine; an
  empty review is a valid result.
- Don't repeat what CI enforces (update-class and Diun labels, profile
  vocabulary, `.env.example` drift). Run the CI scripts once and report only
  their failures.

## Scope

- `$ARGUMENTS` empty: the working tree against `HEAD`, i.e. `git diff HEAD`
  plus every untracked file from `git status --porcelain` (read those in full).
- `$ARGUMENTS` given: `git diff $ARGUMENTS` and `git log --oneline $ARGUMENTS`.

## Checks

1. **Static:** `python3 scripts/ci/check_configs.py` and
   `python3 scripts/ci/check_env_drift.py`. Per changed file type, the
   checks from CLAUDE.md "Verification" (compose `config --quiet`,
   `shellcheck`, JSON/YAML parse).
2. **Deploy effect:** per changed path, what happens on push according to
   CLAUDE.md "Deployment":
   - `mnemosyne/stacks/<stack>/`: find the services that mount or build from
     the file in that stack's `docker-compose.yml`. Name each service that
     gets recreated or rebuilt, and every `manual=` case (gitea,
     gitea-runner, files no service mounts).
   - systemd units, `/etc/docker/daemon.json`, other nodes outside their
     handler's scope: manual step, name it.
   - `clusters/daidalos/`: a push is a Flux deploy; apply
     `.claude/rules/kubernetes.md`.
   - Compare `git diff --stat` with `git diff -w --stat`: a file whose
     changes are whitespace-only still recreates its container.
   - More than one stack touched: say so.
3. **Public mirror:** for added lines in files not matched by
   `scripts/config/.publicignore`, look for private IPs (RFC 1918,
   100.64.0.0/10), internal domains and hostnames listed in
   `CLAUDE.local.md`, the login username (`whoami`) and secrets. Flag any
   change to the mirror tooling (`scripts/export_public.py`,
   `scripts/sync_public.py`, `scripts/audit_repo.py`, `.publicignore`).
4. **Surgical:** every hunk must serve the change's evident purpose. Flag
   reformatting, reordering and drive-by edits, especially in mounted
   configs or other stacks.
5. **Semantics:** read the changed logic, not just its syntax. Typical
   traps here: `$` not escaped as `$$` in compose/env values; PromQL with
   wrong labels or a `rate()` window shorter than two scrape intervals;
   failures hidden with `2>/dev/null` or `|| true` without a stated reason;
   metric, container or label names that don't exist. Run new or changed
   PromQL against the live Prometheus (read-only, see `CLAUDE.local.md`); an
   empty result is a finding.
6. **Follow-ups:** `SERVICES.md`, `RUNBOOK.md`, `ARCHITECTURE.md`, `wiki/`,
   the README "License" section for third-party files, the executable bit on
   new scripts (`git diff` shows `new file mode 100755`). Name Obsidian vault
   notes that the change makes stale (vault map in `CLAUDE.local.md`; read
   only the notes you need, never edit).
7. **Rollback:** does `git revert` undo it cleanly, or does the change leave
   state behind (volumes, schema migrations, networks, data written by a new
   service)?
8. **Security (new exposure only):** flag what the diff *adds*, not what
   already exists.
   - Container privileges: `docker.sock` mount, `privileged`, `cap_add`,
     `network_mode: host`, `pid: host`, running as root where the image
     supports a user.
   - Exposure: new `ports:` bound to `0.0.0.0` instead of `127.0.0.1` or the
     `caddy_proxy` network only; new Caddy sites without auth; anything
     newly reachable through Zephyros or from outside the LAN.
   - Access paths: changes to `authorized_keys`, sudoers, the `claude-ro`
     setup, webhook config, forced-command restrictions (`command=`,
     `restrict`), SSH/Tailscale config.
   - Secrets: values in tracked files instead of `.env`; new secret files
     without a restrictive mode; scripts that print or log env/secrets;
     plaintext `Secret` manifests under `clusters/`.
   - Supply chain: `curl | sh`, images without a tag, new third-party
     images from unknown publishers, `pip`/`npm` installs without a pin.
   - App code (Flask etc.): shell/SQL injection, unvalidated input reaching
     `subprocess`, write endpoints without auth — especially anything that
     can reach a device API (CLAUDE.md "Physical devices").

## Report

Write the report in German (informal "du"); keep identifiers, paths and
commands in English. Use exactly these sections and omit none: write
"keine" for an empty one.

```
## Review: <n> Dateien, Stacks: <list or none>

BLOCKER      — would break a deploy, leak private data or lose data
WARNUNG      — likely wrong or has an unintended side effect
HINWEIS      — worth a look, not wrong

DEPLOY-EFFEKT
- <stack>: <up -d | force-recreate <svc> | build <svc>>
- manual: <steps or none>

NACHZIEHEN
- <docs, license rows, vault notes, chmod>

GEPRÜFT
- <command> → <relevant output, one line>

NICHT GEPRÜFT
- <what and why>
```

Each finding: `file:line` — what is wrong — concrete consequence. Sort by
severity. A security finding is a BLOCKER if it makes something reachable
from outside the LAN or exposes a secret, otherwise a WARNUNG. Don't propose a commit message; don't summarize the diff back.
