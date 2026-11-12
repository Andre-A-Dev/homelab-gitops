---
name: open-points
description: Work the homelab open-points list in the knowledge base. Without an argument, propose the next items; with an item, check its real state, implement it and update the list; "add" files a new point, "done" closes one after deploy.
argument-hint: "[<item> | add <text> | done <item>]"
disable-model-invocation: true
allowed-tools: Bash(git status *) Bash(git diff *) Bash(git log *) Bash(git show *) Bash(ssh mnemosyne-ro *) Bash(ssh boreas-ro *) Bash(ssh hephaestus-ro *) Bash(ssh zephyros-ro *) Bash(ssh daidalos-ro *) Bash(flux get *) Bash(kubectl get *)
---

The open points live in the knowledge base, not in this repo: a prioritized
main list plus review notes with their own "Maßnahmen" lists. Their location
is the row "Open points, reviews" in the knowledge-base table of
`CLAUDE.local.md`. The notes lag behind the repo: points get fixed in passing,
reviews go stale. So every mode starts by checking reality, not by trusting
the note.

## Ground rules

- **Invoking this skill is the request to edit the vault**, limited to the
  open-points and review notes. Other notes that a change makes stale: name
  them in the report, don't edit them unless the user says so.
- **The repo wins.** A point that the repo or the live system shows as done
  is done; record that instead of building it again.
- **Done means deployed and verified.** Implementing a point in this session
  leaves it open with a "Stand" note. Only `done` closes it (see there).
- Live checks read-only, only via the `-ro` aliases (`CLAUDE.local.md`).
  Physical devices and anything outside the sandbox (device settings, `~/.ssh`,
  purchases, router UI) are user steps: say so, never attempt them.
- Vault edits are surgical: change only the line(s) of the item, keep the
  note's formatting, never reorder or reword other items. German prose,
  absolute dates, Obsidian Tasks syntax as used in the note (`- [ ]`,
  `✅ YYYY-MM-DD`, priority 🔺 ⏫ 🔼 🔽), status annotations as
  `— Stand YYYY-MM-DD: …`.
- One item, or one tight block of related items, per session. If the user
  picks several unrelated items, suggest separate sessions.

## Mode: no argument — pick

1. Read the main list and the open "Maßnahmen" of the review notes.
2. Drop items that are blocked (waiting for delivery, external), on hold,
   decisions only the user can make, and pure user steps (devices, purchases).
   List the user steps separately in one line each; they need no session.
3. Rank the rest by the note's own rule (consequence of doing nothing, read
   the priority definitions from the note's header). Ties: smaller and
   self-contained first.
4. Cheap reality check of the top candidates only (one `grep`/`git log` each):
   drop or flag what is already done.
5. Propose 2–3 with: priority, one line why now, rough size, whether a user
   step is involved, and the exact command to start it:
   `/open-points <short item name>`. Stop there; don't start the work in
   this mode. The user sends that command (here or in a fresh session), and
   the `UserPromptSubmit` hook `scripts/session_title.py` names the session
   after the item, so parallel sessions stay distinguishable.

## Mode: `<item>` — work it

Match `$ARGUMENTS` against the item titles in all open-points notes. Several
matches, or the same point in the main list and a review note: list them and
ask. Then:

1. **State check.** Turn the item text into concrete claims (file contains X,
   alert Y missing, mode is 644, service binds 0.0.0.0) and check each against
   the repo (`grep`, `git log -S`/`--grep` for a fix commit) and, where the
   claim is about the live system, read-only on the host. Report per claim:
   still true / fixed (with commit) / not checkable (why).
   - All fixed: propose closing the item (as in `done`), stop.
   - Partly fixed: narrow the scope to what is left and say so.
2. **Context.** Read only what the item needs: the notes it links, the repo
   files it names. Don't load the other review notes.
3. **Conflicts.** If the item collides with a rule in `CLAUDE.md` or
   `.claude/rules/` (example: SOPS is blocked there until the user changes
   that section), stop and name the rule. If it needs a design decision,
   present the options with a recommendation and ask before writing code.
4. **Implement** under the normal rules of `CLAUDE.md`: surgical changes,
   docs in the same change set, verification, diff first if more than one
   stack, commit proposal.
5. **Update the vault.** Append to the item's line:
   `— Stand YYYY-MM-DD: umgesetzt, uncommitted; Deploy offen` plus what is
   left (manual step, test from outside). Leave it unchecked. If the point is
   mirrored between the main list and a review note, update both.

## Mode: `add <text>`

1. Search all open-points notes for an existing entry on the same topic
   (keywords, file names, service names). Found: propose updating that entry
   instead, show the old and new line.
   Also search the ideas folder next to the open-points folder: a project
   enters the list only once the user has decided to do it (the main list's
   header states the rule). An undecided idea: propose a note in the ideas
   folder instead and stop. A decided project with an idea note: the entry is
   one line in P3 linking that note; don't copy the concept.
2. If the text states facts (a mode, a missing alert, a failing job), check
   them cheaply; write "geprüft YYYY-MM-DD" or "nicht geprüft" into the entry.
3. Choose the section: P0–P3 by the definitions in the note's header,
   "Befunde, noch nicht einsortiert" when unsure, "Wartet auf Lieferung /
   extern" when blocked. One line why (consequence of doing nothing).
4. Show the exact line(s) and the target section, ask for confirmation, then
   write. New items go at the end of their section.

## Mode: `done <item>`

For a point whose fix the user has pushed.

1. Verify it is live the way the `deploy-check` skill does (deploy log,
   container state, the metric/alert/route the fix added). Not live or not
   verifiable: report, leave the item open.
2. Find the commit: `git log --oneline --grep` / `-S` on the changed files.
3. Check the box, append `✅ YYYY-MM-DD` and the short commit hash. In the main
   list, move the item to the top of "Kürzlich abgeschlossen" (the note's own
   convention); in review notes, tick it in place. Update the mirrored entry
   in the other note too.
4. Name notes the fix made stale (host overviews, service notes).

## Report

German, informal "du"; identifiers, paths and commands in English.

```
## Open Point: <Titel> (<P-Stufe>)

STAND
- <Behauptung> — noch offen | erledigt in <hash> | nicht prüfbar — <Beleg, eine Zeile>

UMGESETZT / VAULT
- <was geändert wurde, in Repo und Vault>

DEIN SCHRITT
- <manuelle Schritte, Geräte, Deploy>

NICHT GEPRÜFT
- <was und warum>

VERALTETE NOTIZEN
- <Notiz> — <welche Aussage>
```

For `pick`, replace the sections with the candidate list and the user-step
line. End with the commit proposal when the repo changed.
