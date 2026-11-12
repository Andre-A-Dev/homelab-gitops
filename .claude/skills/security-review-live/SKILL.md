---
name: security-review-live
description: Security review of the running homelab, not of a diff. Re-runs the regression checks from earlier reviews, reviews what changed since the last review in the repo and on the hosts, and writes the result as a new review note with measures in the knowledge base. Read-only on repo and hosts.
argument-hint: "[full]"
disable-model-invocation: true
allowed-tools: Bash(git status *) Bash(git diff *) Bash(git log *) Bash(git show *) Bash(python3 scripts/ci/check_configs.py) Bash(ssh mnemosyne-ro *) Bash(ssh boreas-ro *) Bash(ssh hephaestus-ro *) Bash(ssh zephyros-ro *) Bash(ssh daidalos-ro *) Bash(kubectl get *) Bash(flux get *)
---

`repo-review` and the built-in `/security-review` look at a diff. This skill
looks at the state of the whole system: what is reachable, who can control
what, which secrets are readable, and whether fixes from earlier reviews still
hold. A `chmod`, a new stack or a router change can silently reopen a closed
finding; nothing else checks for that.

The concrete checks live in `checks.local.md` next to this file. It names
where the homelab had gaps, so it is private and excluded from the public
mirror. **If it is missing, stop**: this copy is the public one and cannot
run.

- `$ARGUMENTS` empty → regression checks plus a review of everything that
  changed since the last review.
- `$ARGUMENTS` = `full` → additionally sweep every stack, Caddy site and host,
  not only the changed ones. Use it after big changes (new host, router or
  network rework) or when the last full sweep is old.

## Ground rules

- **Read-only everywhere.** Live access only via the `-ro` aliases and the
  read-only kubeconfig (`CLAUDE.local.md`). Device APIs: `GET` on the info
  endpoint only, never anything that switches, configures or reboots.
- **Never read secrets.** Modes and owners via `stat`, never `cat`, `tar -t`,
  `printenv` or log dumps of files that may hold credentials. Don't quote
  secret values, tokens or hashes in the note or the report. `Permission
  denied` for `claude-ro` on a secret path is the desired result, not a
  failure.
- **Not checkable is a result.** Checks that need root, the user's home, the
  router UI, a provider console or a vantage point outside the network are
  user steps: list them with the exact command, never skip them silently.
- `ssh <host>-ro '…'` with `$(…)` in the remote command runs inside the
  sandbox and fails with "Could not resolve hostname". Use globs, `xargs` or
  a second command.
- **Invoking this skill is the request to write one new review note** in the
  open-points folder of the knowledge base (location: row "Open points,
  reviews" in `CLAUDE.local.md`). Don't edit other notes; name the ones that
  are now stale. The main open-points list is updated by the user or the
  `open-points` skill, not here.
- `checks.local.md` lives under `.claude/skills/`, which the sandbox does not
  let you write. Propose changes to it as text for the user.

## 1. Baseline

1. Find the newest security review note in the open-points folder (title or
   file name contains "Security"). Take its date, its HEAD, its open measures
   and its "Nicht verifiziert" list.
2. Read `checks.local.md`.
3. `git log --since=<review date> --format='%h %ad %s' --date=short
   --name-only` gives the change set to review in step 3.

## 2. Regression checks

Run every check in `checks.local.md`. Record per check: `ok`, `regression`,
`known-open`, `changed` or `not checkable`, each with one line of real output
as evidence. A `regression` is the most important result of the whole review:
it means a fix did not hold. Find the commit or event that reopened it
(`git log -S`, file mtime, container age).

## 3. Review of what changed

For each change since the last review (all of them with `full`), ask what
it adds to the attack surface:

- **Compose services:** published ports (anything not bound to `127.0.0.1`),
  `docker.sock`, `privileged`, `network_mode: host`, `cap_add`, host paths
  mounted read-write, image tag policy (`CLAUDE.md` "Update classes").
- **Caddy sites:** `.home` without `lan_only` (CI covers Mnemosyne; check
  other hosts by hand), endpoints that write (POST, admin APIs, device
  control) without auth, `tls_insecure_skip_verify`, header injection of
  credentials into every request.
- **Applications:** wildcard CORS, missing auth on state-changing routes,
  CSRF on routes a foreign web page could trigger from the user's browser,
  endpoints that cost money (LLM APIs) or switch hardware.
- **Scripts and units:** credentials in `/tmp` or world-readable files,
  tokens in URLs or argv, TLS verification off, `StrictHostKeyChecking=no`,
  missing `umask`, root units without hardening.
- **CI:** `permissions:`, actions pinned by tag only, secrets reachable from
  untrusted triggers, runners with host access.
- **Hosts and network:** new listeners against the port baseline in
  `checks.local.md` (`ss -ltnH` on every host), new devices, new port
  forwards or DNS records (user step for the router side).
- **Public mirror:** new files with personal data or real identifiers that
  `.publicignore` does not cover (the `mirror-review` skill checks syncs; here
  only flag new candidates).

Rate each finding by the consequence if exploited, as the earlier reviews did:

- **Kritisch:** reachable from the internet, root on a host, loss of data
  and backups.
- **Hoch:** anyone in the LAN or tailnet controls a service or device without
  auth, or reads secrets.
- **Mittel:** needs a further step (local user, own browser, compromised
  device) or weakens a defence.
- **Niedrig:** hygiene, defence in depth.

Verify every claim before writing it down. A finding you could not reproduce
goes under "Nicht verifiziert", not under a severity.

## 4. Earlier measures

For every open measure of the earlier review notes: `offen`, `erledigt`
(commit or evidence) or `teilweise` (what is left). Repo and live state win
over the note.

## 5. Write the review note

New note in the open-points folder, file name `<next free number>_Security_
Review_<YYYY-MM-DD>.md`, German prose, same structure as the earlier reviews:

- Frontmatter: `aliases`, `tags` (`homelab`, `security`, `review`,
  `homelab-infra`), `created`, `updated:` empty, `status: Offen`.
- Callouts: purpose (scope, HEAD, `full` or not), "Doku ≠ System",
  "Nie ins Public-Repo".
- Zusammenfassung: table of the results, regressions first.
- Regressionen (if any), then findings by severity, each with
  Verifiziert / Szenario / Fix.
- Regressions-Checks: one table, ID → result → evidence.
- Status der früheren Reviews: table, one row per open measure.
- Nicht verifiziert, with the user step that would settle each.
- Maßnahmen: Obsidian Tasks, ordered by consequence of doing nothing,
  priority 🔺 ⏫ 🔼 🔽.
- Verwandt: links to the earlier review notes and the main list.

## 6. Hand-over

- **Proposed changes to `checks.local.md`:** a new check for every new
  finding (ID, origin, expected, command you ran), updated baselines where a
  change was deliberate, `known-open` → `ok` where a measure is done. Only
  commands you actually ran.
- **CI candidates:** checks that only read the repo and have a clear
  pass/fail belong in `scripts/ci/check_configs.py`, where they run on every
  push. Name them; implementing is a separate change set.
- **Stale notes:** knowledge-base notes the results contradict.

## Report

German, informal "du"; identifiers, paths and commands in English.

```
## Security Review <YYYY-MM-DD>: <keine Befunde | n Befunde | Regression>

REGRESSIONEN
- <ID> — what reopened — evidence — likely cause

NEUE BEFUNDE
- <Kritisch|Hoch|Mittel|Niedrig> <title> — one line

DEIN SCHRITT
- <check or fix only the user can do> — <command>

VORSCHLÄGE
- checks.local.md: <additions/changes>
- CI: <candidates>

NICHT GEPRÜFT
- <what and why>

NOTIZ
- <path of the new review note>; stale: <notes>
```
