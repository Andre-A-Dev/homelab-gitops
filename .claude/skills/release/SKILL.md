---
name: release
description: Prepare the next release of homelab-infra. Summarize the commits since the last tag, pick version and title, and draft the CHANGELOG.md section that becomes the Gitea and GitHub release notes. Edits only CHANGELOG.md; never tags, commits or pushes.
argument-hint: "[version, default: next minor]"
disable-model-invocation: true
allowed-tools: Bash(git status *) Bash(git diff *) Bash(git log *) Bash(git show *) Bash(git rev-parse *) Bash(git describe *) Bash(git tag -l *)
---

You prepare a release in the private repo. Every push to `main` deploys and
nobody pins versions, so a release is a milestone, not a deployable artifact.
It serves two readers: the user's own changelog in Gitea, and the public
GitHub mirror as a showcase of the project.

Read `CLAUDE.md` and `CLAUDE.local.md` first: they hold the general rules
(you never commit, fish syntax, German replies).

## Conventions

- Version `v1.MINOR.0`: one minor per release, no patch releases, the same
  number in Gitea and the mirror. `$ARGUMENTS` overrides the version.
- Title: the theme in a few words (`Viessmann Heating Integration`).
- `CHANGELOG.md` at the repo root is the only source of the notes. Both
  release bodies are copies of its section. The public one comes from the
  mirror's `CHANGELOG.md`, which `sync_public.py` has sanitized and
  `mirror_check.py scan` has checked.
- Write for a public reader from the start: no IPs, internal domains,
  usernames or private commit hashes, and no detail that helps an attacker
  (which services were reachable without auth, exact device commands). The
  sanitizer is the safety net, not the plan.
- Tag messages carry only `vX.Y.0 — <Title>`. The `v1.1.0` tag message holds
  the full notes including an internal IP; tags are not sanitized.

## Steps

1. **State.** `git status --porcelain` must be empty and `HEAD` equal to
   `origin/main` (local ref, no fetch), so the changelog commit stands alone
   on pushed history. Last release: `git describe --tags --abbrev=0`.
   Otherwise report and stop.
2. **Collect.** `git log <last>..HEAD --no-merges --format='%h %ad %s'
   --date=short`; `git show --stat` where a subject is unclear.
3. **Judge.** A release needs at least one finished theme a reader of the
   public mirror would notice: a new host, stack or capability working end
   to end. Hardening, tooling and workstation changes alone don't qualify;
   they ride along with the next one. Exception: more than about three
   months since the last tag and the range holds a finished security or
   reliability pass. Half-done work (a planned feature with only its first
   commits) waits. If nothing qualifies, say so and stop; "not yet" is a
   valid result.

4. **Draft** the section at the top of `CHANGELOG.md`, below the header:

   ```
   ## v1.2.0 — <Title> (YYYY-MM-DD)

   <2-4 sentences: what the homelab can do now that it couldn't before, and why>

   ### <Theme>

   - <outcome in one line>
   ```

   - Themes by what changed for the homelab (Monitoring, Backups, Security,
     Kubernetes, Hosts, Tooling ...), not by commit type.
   - Bullets name outcomes, not commits. Fold fix-ups into the feature they
     fixed; drop chores, typo fixes and work reverted within the range.
     At most about eight bullets per theme.
   - Date: today. If the user tags on another day, the tag date wins; tell
     them to adjust it.
   - `CHANGELOG.md` missing: create it with this header, then add sections
     for the older tags from their messages
     (`git tag -l --format='%(contents)' <tag>`), rewritten by the rules
     above:

     ```
     # Changelog

     Milestones of this homelab. Every push to `main` deploys, so a release
     marks a finished theme, not a deployable version. Releases before
     v1.2.0 predate the public mirror and have no tag there.
     ```

5. **Report** (German, see `CLAUDE.md`): version, title, and per bullet the
   short hashes it stands for, so the user can check the summary against the
   history. The mapping goes into the report, not into the file.
6. **Hand-over**, in fish, with the real version and title filled in:
   1. Commit `CHANGELOG.md` alone as `docs(changelog): Add v1.2.0` and push.
      The deploy handler ignores `*.md`.
   2. Private tag on that commit:
      `git tag -a v1.2.0 -m 'v1.2.0 — <Title>' <changelog-commit>`, then
      `git push origin v1.2.0`. Tag pushes trigger no workflow (all trigger
      on `branches: [main]`).
   3. Gitea: Releases → New Release → existing tag `v1.2.0`, title
      `v1.2.0 — <Title>`, body: the section without its heading line.
   4. Mirror, once a sync containing the changelog commit is pushed: find
      the commit with `git log --format='%h %s' --grep='Add v1.2.0'
      github/main`, tag it the same way and `git push github v1.2.0`.
      GitHub: Releases → Draft a new release → existing tag, body from the
      **mirror's** `CHANGELOG.md`, not the private one.
   5. `/mirror-review commits` afterwards: `preflight` accepts tags on
      `github/main` and fails on any other, `scan` checks tag messages.
7. **After the user says it's done:** `git tag -l --format='%(refname:short)
   %(objecttype) %(contents:subject)' v1.*` and `git ls-remote --tags origin`
   show the annotated tag locally and on Gitea. The GitHub side is checked
   by `mirror-review` (local refs only, no GitHub fetch).
