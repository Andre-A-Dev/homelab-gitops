---
name: deploy-check
description: Verify a pushed change on the live systems after the user says it is deployed or live. Read-only; reads deploy logs, container state, Prometheus and Flux, reports what happened and which manual steps remain.
argument-hint: "[commit SHA, default: origin/main]"
allowed-tools: Bash(git status *) Bash(git diff *) Bash(git log *) Bash(git show *) Bash(git rev-parse *) Bash(ssh mnemosyne-ro *) Bash(ssh boreas-ro *) Bash(ssh hephaestus-ro *) Bash(ssh zephyros-ro *) Bash(flux get *) Bash(kubectl get *) Bash(kubectl describe *) Bash(kubectl logs *)
---

The user pushed a change and says it is live. Check what the deploy actually
did and whether the change works. "Applied" is not "working"; an empty result
is a finding, not a success.

## Ground rules

- **Read-only.** Live access only via the `-ro` aliases and the read-only
  `claude-ro` kubeconfig, as described in `CLAUDE.local.md`. Never call the
  deploy webhook, never restart, recreate or reconcile anything, never `git
  fetch` (the user pushed; the local `origin/main` is current. If they pushed
  from another machine, ask for the SHA).
- Don't ask for a recreate the handler already did. Name a manual step only
  for what no handler covers.
- If this session made the change, use what you know about it (new metric,
  new alert, new route) for the functional check in step 5.

## 1. Scope

- Target: `$ARGUMENTS` if given, else `git rev-parse --short origin/main`.
- Base: the `sha=` of the previous `result=` line on Mnemosyne (step 2).
  Changed paths: `git diff --name-only <base>..<target>`; commits:
  `git log --oneline <base>..<target>`.
- Group the paths by host: `mnemosyne/`, `boreas/`, `hephaestus/`,
  `zephyros/`, `clusters/daidalos/`, `shared/` (symlinked on every Pi, live
  as soon as that Pi has pulled). Skip hosts with no changed path. Docs-only
  changes (`*.md`, `wiki/`, `.claude/`): say so and stop after step 2.

## 2. Mnemosyne deploy log

```
ssh mnemosyne-ro 'journalctl -u webhook --since "-6h" --no-pager -o cat | grep -E "event=|result="'
```

The handler writes `event=` lines per stack and one summary line per run:
`result=ok|failed|noop sha=<short> applied="..." manual="..." failed="..."`.

- No `result=` line for the target: the job has not reached the webhook
  (validate failed, runner busy, job still running). Say so and point to the
  Actions run in Gitea; don't guess.
- `result=noop`: a later run already deployed it; check that run instead.
- `failed=`: report the `event=` lines of that stack (`config_error`,
  `deploy_failed`, `unexpected_exit`) verbatim.
- `manual=` entries and what they mean:
  - `gitea`, `gitea-runner`: never applied by the handler (recreating kills
    the running job). Manual `docker compose up -d` in `~/stacks/<n>`.
  - `<stack>:unmapped`: changed files no running service mounts or builds
    from. Find out who uses them (systemd timer script, cron, a mount that was
    removed) and say whether the change is live already (script called by
    path at the next run) or needs a step.
  - `<stack>:inactive-leftovers`: containers of a profile-deactivated service
    still exist. Manual `docker compose rm -sf` in `~/stacks/<n>`.
  - `<stack>` with `reason=manual|no-compose`: name the stack and why.

Not deployed by anything, independent of the log: changed systemd units
(`daemon-reload` + restart or `enable --now` on the host), `mnemosyne/docker/
daemon.json`, `.env`. A key added to `.env.example` needs a value in the
host's `.env` before the service can use it: check the service logs for a
missing-variable error.

## 3. Containers of applied stacks

```
ssh mnemosyne-ro 'sudo -n docker ps -a --filter label=com.docker.compose.project=<stack> --format "{{.Names}}\t{{.Status}}\t{{.Label \"com.docker.compose.service\"}}"'
```

- Every recreated or rebuilt service must show `Up` with an age that matches
  the deploy time. `Up 3 hours` after a 10-minute-old deploy means it was not
  recreated: a finding.
- `Restarting`, `Exited`, `(unhealthy)`, `health: starting` well after the
  deploy: finding.
- Logs since the deploy, errors only:
  `ssh mnemosyne-ro 'sudo -n docker logs --since <deploy time> <container> 2>&1 | grep -iE "error|fatal|panic|fail" | tail -20'`.
  No match is fine; quote matches verbatim. Never dump full logs into the
  report (they can contain tokens).

## 4. Other hosts

**Boreas, Hephaestus, Zephyros** (webhook pull plus 15-minute cron):

```
ssh <host>-ro 'tail -n 60 /var/log/webhook-handler.log'
ssh <host>-ro 'journalctl -t gitea-pull --since "-1h" --no-pager -o short-iso'
```

- The last webhook block lists the changed files of the push: their presence
  proves the pull. `ERROR:` lines in that block are findings.
- `gitea-pull` errors before a later successful pull are transient (Gitea or
  Tailscale briefly unreachable): mention only if the latest attempt failed.
- The handler acts only on: Boreas `pihole6-exporter.service`, Hephaestus
  `vcontrold.service`, Zephyros `caddy/` (validated first) and `monitoring/`.
  Every other changed file on these hosts is a manual step; name it.
- Zephyros containers: `ssh zephyros-ro 'sudo -n docker ps --format "{{.Names}}\t{{.Status}}"'`.
- `claude-ro` cannot read the repo checkout on these hosts.

**Daidalos** (Flux, only if `clusters/daidalos/` changed): follow "After I
pushed" in `.claude/rules/kubernetes.md`. The `flux-system` revision must
equal the target SHA and be `Ready`; an older revision means not yet
reconciled, not failed. Name the Flux interval from
`clusters/daidalos/flux-system/gotk-sync.yaml` and stop there; never reconcile.

## 5. Monitoring and functional check

Prometheus API, see `CLAUDE.local.md`:

- `up == 0`: compare with the targets touched by the change; a target that
  was down before the deploy is not caused by it, say so.
- Firing alerts: `curl -s localhost:9090/api/v1/alerts`. Ignore `Watchdog`.
- Alert rules changed: `curl -s localhost:9090/api/v1/rules`; every rule
  must have `health: ok`, and the new or changed rule names must be loaded.
- New metric, exporter or target: the series exists and has plausible
  values; the target is `up`.
- New or changed Caddy route: `curl -sk -o /dev/null -w "%{http_code}"` from
  Mnemosyne against the internal URL (GET only).
- Physical devices (PrusaLink, Shelly): GET only, never any write.

## Report

German, informal "du"; identifiers, paths and commands in English. Exactly
these sections, "keine" for an empty one:

```
## Deploy-Check <short sha>: <ok | Befunde | unvollständig>

BEFUNDE
- <host/stack> — what is wrong — evidence (one line of real output)

DEPLOY
- Mnemosyne: applied=<...> manual=<...> failed=<...>
- <host>: pulled <time>, <what the handler did>
- Daidalos: <revision, Ready>

MANUELL NÖTIG
- <host>: <command, bash for servers> — <why no handler covers it>

GEPRÜFT
- <command> → <relevant output, one line>

NICHT GEPRÜFT
- <what and why>
```

Don't summarize the diff back, don't propose commits. If a finding needs a
fix in the repo, name it in one line; the fix is a new change set.
