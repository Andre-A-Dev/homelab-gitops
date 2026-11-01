---
name: container-updates
description: Review pending container image updates on Mnemosyne (digest updates, new tags beyond the pin, class C rebuilds), resolve old and new versions, read the release notes and produce an ordered update plan; afterwards verify the result. Read-only; the user runs the commands.
argument-hint: "[verify]"
allowed-tools: Bash(python3 .claude/skills/container-updates/scripts/update_check.py *) Bash(ssh mnemosyne-ro *) Bash(git status *) Bash(git diff *) Bash(git log *) Bash(git show *) WebFetch
---

You prepare the container part of the monthly update window. The user runs
every command on the server; you never pull, recreate or restart anything.
Commands for the server are bash.

Read `wiki/Update-Strategy.md` first. It defines the classes, the tag rules,
the window order, the exceptions and the verification pitfalls. This skill
only adds how to turn the current state into a plan. Where the two disagree,
the wiki wins; tell the user about the drift.

- `$ARGUMENTS` empty → **A. Review and plan**
- `$ARGUMENTS` = `verify` → **B. Verify after the update**

## A. Review and plan

### 1. Collect

```
python3 .claude/skills/container-updates/scripts/update_check.py
```

It prints four sections; take them as facts, don't re-derive them by hand:

- **Digest updates:** the pinned tag points to a new image. Per container the
  running and the available version, build date, source repo. `unknown`
  means neither the OCI label nor a tag match resolved the digest; say so,
  don't guess a version. "already up to date" means the user updated after
  the metric ran (the metric runs once a day).
- **New tags beyond the pin:** what Diun reports by mail and ntfy for
  `diun.watch_repo` services, recomputed from the registry with the
  service's `diun.include_tags`.
- **Class C:** running local builds; their base images are never checked.
- **Registry errors:** images the metric could not check.

Cross-check with Diun, which runs on its own schedule and may already have
seen an image the daily metric has not:

```
ssh mnemosyne-ro 'sudo -n docker logs --since 168h diun 2>&1 | sed "s/\x1b\[[0-9;]*m//g" | grep -E "found"'
```

Diun's log only reaches back to its last container start. For an image Diun
lists that the report does not: `update_check.py --image <ref>`.

Only Mnemosyne is covered. Containers on Boreas, Zephyros and Hephaestus are
watched by nothing; name that in the report, don't check them.

### 2. Classify every finding into exactly one path

| Finding | Path |
|---|---|
| Digest update, same version (rebuild, e.g. `-ls407` → `-ls408`, or label equal) | Pull in the window. Base image fixes only; no release notes needed. |
| Digest update, new patch/minor inside the pin | Pull in the window after reading the notes (step 3). Class A: backup first. |
| New tag beyond the pin | Repo change, not a pull. One commit per stack: image tag **and** the lower bound of `diun.include_tags` together (CLAUDE.md, "Update classes"). Follows "Major and minor upgrades" in the wiki. Offer to prepare the diff; it then goes through the normal change-set flow. |
| Class C | `docker compose build --pull && docker compose up -d` per stack in the window. |
| Registry error | Report; look at `docker logs` only if the user asks. |

Known special cases (details in the wiki):

- **Nextcloud:** always the whole stack, never single services.
- **gitea, gitea-runner:** recreating either kills a running Actions job.
  Only when no job runs; the deploy handler never touches them.
- **Databases** (class A, `mysql`, `mariadb`, Immich's postgres): a patch
  inside the pinned minor is a pull after `verify-backup.sh`; an engine minor
  or major is a commit plus a cold copy of the data directory.
- **Floating `latest`** (single-maintainer exporters, Vaultwarden): the
  version jump can be large; read the notes over the whole range.

### 3. Release notes

For every update with a version change, read the notes for the whole range
`running → available`, not just the newest release. Source: the `source`
line, otherwise the project's GitHub releases page (WebFetch). Look for:

- breaking changes, removed or renamed config options and env variables:
  grep the stack's compose file and mounted configs for every removed name
- database or schema migrations, minimum versions of dependencies
- security fixes (raise the priority, name the CVE)

If the running version is `unknown`, say which range you could not read and
why. Since `container-update-metrics.sh` writes `local_digest`, this should
only happen for images without version tags.

### 4. Plan

Order as in the wiki window: backup check first, system packages before
containers, nothing between 02:00 and 04:30.

Report in German, informal "du"; identifiers, paths and commands in English.
Exactly these sections, "keine" for an empty one:

```
## Container-Updates <date>: <n> Updates, <m> neue Tags

ZUERST
- sudo verify-backup.sh   (if any class A update is in the plan)

IM FENSTER (Reihenfolge)
- <stack>: <old> → <new>, class <X> — <one line: rebuild | notes summary | risk>
  cd ~/stacks/<stack> && docker compose pull <svc...> && docker compose up -d <svc...>

KLASSE C
- cd ~/stacks/<stack> && docker compose build --pull && docker compose up -d

NICHT IM FENSTER (Commit nötig)
- <stack>: <pin> → <new tag> — what the notes say, what the commit changes

ZURÜCKSTELLEN
- <update> — why (breaking change, waiting for a dependency, ...)

DANACH
- /container-updates verify, then docker image prune

NICHT GEPRÜFT
- <what and why: unknown versions, uncovered hosts, ...>
```

## B. Verify after the update

The metric runs once a day, so it cannot confirm today's update yet. Check
directly:

1. **Old image still running** (wiki, "Verification pitfalls"):
   `ssh mnemosyne-ro 'sudo -n docker ps --format "{{.Names}}\t{{.Image}}\t{{.Status}}"'`.
   A bare 12-character image ID instead of the tag means the container still
   runs the old image: `docker compose up -d --force-recreate <svc>`.
2. **Recreated:** every updated container shows an `Up` age that matches the
   window; nothing `Restarting` or `unhealthy`.
3. **Logs since the restart**, errors only:
   `sudo -n docker logs --since 1h <container> 2>&1 | grep -iE "error|fatal|panic|migrat" | tail -20`.
   Migration lines are expected for class A; quote them.
4. **Prometheus:** `up == 0` and `probe_success == 0` return only known
   exceptions; no new firing alert besides `Watchdog`.
5. **Version inside the container:** the startup lines in the logs, or an
   unauthenticated GET where the app exposes one (Gitea's `/api/v1/version`
   needs a login). Otherwise tomorrow's metric run: `update_check.py` should
   then no longer list the container, and its `local_digest` resolves to
   the new version.

Then `docker image prune` is safe: the old images were the only quick way
back. Report like `deploy-check` (BEFUNDE, GEPRÜFT, NICHT GEPRÜFT).
