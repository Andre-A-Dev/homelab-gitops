# Personal preferences (all projects)

## Communication

- Talk to me in German, informal "du". Don't address me by name.
- Code, comments, dashboards and identifiers in English.
- Be direct, no sugar-coating. If something is a bad idea, say so and name the
  trade-off.
- Think ahead: briefly point out consequences and follow-up steps I will need
  (manual restarts, data migration, docs to update).
- If my request is ambiguous, name the ambiguity and ask before acting.

## Engineering

- No complexity without concrete benefit.
- Verify, don't assume. State what you verified and what you didn't.
- Unsure about a tool's syntax: check its docs or `--help` first. For
  proprietary tools without public docs, ask me for documentation instead of
  guessing.
- Do not commit by yourself. Write me commit messages. I will code and changes myself and will commit it myself.

## Shell

- My workstation uses **fish**. Commands for me to paste: fish syntax, or wrap
  bash-isms (heredocs, `for ... | ...`, `export`) in `bash -c '...'`.
  Remember: `set -x VAR value` in bash enables tracing, it doesn't set a variable.
- Servers use bash.
- New scripts: `chmod +x` plus `git add --chmod=+x`.
