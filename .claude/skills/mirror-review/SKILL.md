---
name: mirror-review
description: Review a sync into the public GitHub mirror and propose commits that follow the private history; with "commits", check the user's commits before push. Run in the mirror checkout. Read-only on the mirror; never commits or pushes.
argument-hint: "[commits]"
disable-model-invocation: true
allowed-tools: Bash(git status *) Bash(git diff *) Bash(git log *) Bash(git show *) Bash(python3 .claude/skills/mirror-review/scripts/mirror_check.py *) Bash(shellcheck *) Bash(flux build *)
---

You work in the checkout of the public mirror. `sync_public.py` in the private
repo has copied a sanitized tree into it; the user commits and pushes by hand.
Your job: make sure nothing private or broken goes public, and turn the sync
into commits that read like the private history.

The private repo is at `$MIRROR_PRIVATE_REPO`, default
`~/Projects/Gitea/homelab-infra`; `preflight` prints the path it resolved.
Below, `<private>` stands for that printed path: the variable is usually
unset, so don't paste it into a command. Read its `CLAUDE.md`, `CLAUDE.local.md` and
`.claude/rules/*.md` first: they hold the general rules (you never commit,
fish syntax, German replies) and the internal context for live checks. This
skill only adds what is specific to the mirror.

`mirror_check.py` (next to this file) does the deterministic checks. It takes
its leak patterns from `REPLACEMENTS` in the private `sync_public.py` at
runtime, so they never appear in this public skill. Prefer its output over
re-deriving the same checks by hand.

- `$ARGUMENTS` empty → **A. Review and commit proposal**
- `$ARGUMENTS` = `commits` → **B. Check the user's commits**

## Things that went wrong before

These shaped the steps below; keep them in mind when something looks off.

- A `remote set-url origin …` meant for the private repo hit the mirror,
  because `$repo` came from `git rev-parse --show-toplevel` in the wrong
  terminal. VS Code "Sync" then fetched the private history and tags. Only a
  missing pull strategy stopped a rebase onto private `main` and a push of the
  mirror commits to Gitea, where they would have deployed. That is why
  `preflight` runs first and stops the review on any FAIL.
- The sanitizer turned a real CIDR into an unparsable placeholder. Fix such
  artifacts in the rule in `sync_public.py` and sync again. Editing the mirror
  by hand is lost on the next sync.
- `sync_public.py` copies the private **working tree**, not `HEAD`.
  Uncommitted private changes reach the mirror.
- Commit messages got attached to the wrong content twice (same subject on
  two commits). `commits` flags repeated subjects.
- `git rebase -i` fails without a configured editor, and a todo list pasted
  into fish runs every line as a command (`(caddy)`, `(backup)` exist there).
- Splitting a sync with `git add -p` attached hunks of shared files
  (`SERVICES.md`, `appliance-alerts.yml`) to the wrong commit; the rebuild
  script avoids that.
- Any `git ls-remote`/`fetch` against GitHub opens a passphrase dialog the
  user may not see. Use the local tracking ref instead.

## A. Review and commit proposal

1. **Preflight:** `python3 .claude/skills/mirror-review/scripts/mirror_check.py preflight`.
   On FAIL, stop and report the fix (fish commands); do not continue the
   review on a mirror that tracks the wrong remote or holds private refs.
   An unreadable private repo is a FAIL too: the fix is the read-only
   `.claude/settings.local.json` in the mirror (RUNBOOK "Harden a
   Workstation for Claude Code"), not `/add-dir`, which grants write access.
2. **Scan:** `… mirror_check.py scan`.
   - Sync dry-run: `0 written` means the working tree equals the sanitized
     private tree. Otherwise the user has not synced yet, or the private repo
     moved on; say which files differ and stop before proposing commits for a
     half-synced state.
   - Leak hits are blockers. The script masks the value; open the line
     yourself to judge it, but don't quote private values in your report.
   - Artifacts and symlinks: blocker, fix at the source (see above).
   - Every untracked file must be explained by a private commit. A file with
     no counterpart is a stray (the root `ssh-agent.conf` once was).
3. **Map the diff to private commits.** `git -C <private> log
   --format='%h %ad %s' --date=short --name-only` from the last synced
   commit on. Find that commit by its subject: the newest mirror commit
   normally repeats it. Private commits that only touch blocklisted paths
   have no mirror counterpart; skip them.
4. **Review as public content.** The changes are usually already deployed
   from the private repo, so focus on what changes by being public:
   - Static checks on the mirror files: `shellcheck` on changed scripts,
     JSON/YAML parse, `docker compose config --quiet`, and
     `flux build kustomization …` for `clusters/`. Placeholders can break
     syntax that is fine in the private repo (an unquoted `<user>` in shell
     is a redirection).
   - Links in docs that point to blocklisted files (they 404 publicly).
   - New third-party files need a row in the README "License" section.
   - Executable bit on new scripts.
   - Live effect, read-only, as `CLAUDE.local.md` describes (`-ro` aliases,
     `claude-ro` kubeconfig, Prometheus API): confirm the private change
     works, since the mirror documents it as done.
5. **Propose commits.** One per private commit; merge only trivial ones.
   - Files touched by more than one commit: don't split them by hand.
     Propose a rebuild of the whole sync instead (B.4: plan dir, test in a
     throwaway clone, hand-over); it replaces the staging blocks below.
   - Check coverage before you answer: every path from `git status
     --porcelain` appears in exactly one block.
   - Per block: the staging commands (fish, `git add -- …`; `git add` on a
     deleted file stages the deletion), then the message in its own code
     block.
   - Messages: take the private subject and body, then rewrite for a public
     reader: no private commit hashes, no private values, and no detail that
     helps an attacker (which services were reachable without auth, exact
     device commands). Keep the why. Conventional Commits, English, no
     Co-Authored-By.

## B. Check the user's commits (`commits`)

1. `preflight`, then `… mirror_check.py commits` (add `--tree <ref>` after a
   rebuild to prove the content did not change).
2. Per commit, compare files and message with the proposal from this
   conversation, or else with the private commits. A repeated subject or a
   message that doesn't describe the files is a finding.
3. `scan` again: `0 written` plus a clean working tree means everything is
   committed and matches the private state; the message scan covers the
   unpushed commits.
4. **Fixing unpushed commits:** rebuild instead of rebase.
   - Write a plan dir to the session scratchpad: `plan.txt` with
     `<msg-nr> <private-commit>...` per line (`#` comments) and one message
     file per commit (`msgs/<msg-nr>.txt`). Group private commits on one
     line only when one of them would break a checkout alone.
   - `<private>/.claude/skills/mirror-review/scripts/rebuild.sh <mirror>
     <upstream-sha> <plan-dir>` syncs each private commit with
     `sync_public.py` and commits exactly the files that commit touched. It
     refuses to start unless HEAD is the upstream SHA and the tree is clean,
     and aborts on leftovers. Run the copy in `<private>`, never the one in
     the mirror: the rebuild rewrites the mirror's copy while it runs.
   - Test it in a throwaway clone (`git clone -q <mirror> <scratch>/…`,
     `git -C <clone> switch -q -C rebuild <upstream>`). Check: commit count,
     no repeated subjects, each `HEAD^{tree}` equal to a probe sync of its
     private commit, the final tree equal to the mirror working tree
     (`diff -r -x .git`, exec bits), and a leak scan of `git log -p`.
   - Hand the user, in fish: `git reset --hard github/main`, removal of the
     untracked sync files, then the script call with absolute paths and the
     expected final tree hash.
   - Already pushed: don't rewrite; name the mistake and move on.
5. After the push, verify with local refs only: `git status -sb` shows no
   ahead/behind and `git rev-parse HEAD` equals the upstream ref.

## Report

German, informal "du"; identifiers, paths and commands in English. Lead with
the outcome (ready to commit or push / what blocks it). Then the findings
with `file:line`, the proposal or the commit check, and at the end what you
verified (command → relevant output) and what you could not verify and why.
