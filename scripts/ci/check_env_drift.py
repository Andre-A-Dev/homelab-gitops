#!/usr/bin/env python3
"""Compare docker-compose.yml variable references against .env.example.

Run locally before pushing:

    python3 scripts/ci/check_env_drift.py

This is the single source of truth for env completeness. The equivalent shell
step in validate.yml was removed: two grep/regex implementations of the same
rule drift apart, and when they disagree there is no way to tell which one is
right.

Three checks:
    missing       referenced in compose, not declared in .env.example
    dead          declared in .env.example, never referenced in compose
    undocumented  compose references variables but no .env.example exists
"""

import argparse
import os
import re
import sys
from collections import Counter

# Only a sanity check that --root points at the repository. The walk covers the
# whole repo: stacks on Zephyros, Boreas, Hephaestus and AstraeusNX were never
# checked while the walk was limited to this directory.
STACKS_ROOT = "mnemosyne/stacks"

# Untracked tool directories. Packages ship their own .env.example files, which
# would show up as stacks without a compose file.
SKIP_DIRS = {".git", "node_modules", ".venv"}

# Supplied by the shell, by Docker or by Compose itself. These are never
# declared in .env.example, so their absence is not a documentation gap.
NOISE = {
    "PWD", "HOME", "PATH", "USER", "SHELL", "HOSTNAME", "TZ", "LANG",
    "COMPOSE_PROJECT_NAME", "COMPOSE_FILE", "DOCKER_HOST",
}

# $VAR and ${VAR}, but NOT $$VAR. $$ is Compose's escape for a literal dollar
# sign: it is passed through to the container and is not a Compose variable.
# The previous pattern had no lookbehind, so Immich's Chksum="$$(psql ..." was
# reported as a variable named "C" - which is why the old noise list had to
# blacklist every single letter of the alphabet. With the lookbehind that
# crutch is unnecessary; a single-letter name showing up now is a real finding.
REF_RE = re.compile(r"(?<!\$)\$\{?([A-Z_][A-Z0-9_]*)")

# Every Compose reference, with its optional modifier. Optionality is decided
# per reference, not per name: ${VAR:-x} in one place does not make a bare
# ${VAR} elsewhere optional.
#   ${VAR:-x} / ${VAR-x}  default supplied        -> optional
#   ${VAR:+x} / ${VAR+x}  only used if VAR is set -> optional
#   ${VAR:?e} / ${VAR?e}  Compose aborts if unset -> REQUIRED
# The previous version treated :? as optional, so a variable guarded with
# :? (the strictest form) was exempt from the .env.example check.
MOD_RE = re.compile(r"(?<!\$)\$(?:\{([A-Z_][A-Z0-9_]*)(:?[-?+])?|([A-Z_][A-Z0-9_]*))")
OPTIONAL_MODS = {"-", ":-", "+", ":+"}

# $$VAR is resolved by the container's shell, not by Compose, so the variable
# still has to exist in the container environment - it is just not a
# Compose-level reference. Tracked separately so a key used only in escaped
# form is not reported as dead.
ESCAPED_RE = re.compile(r"\$\$\{?([A-Z_][A-Z0-9_]*)")

# Matches a real env_file key, not the word appearing in a comment.
ENV_FILE_RE = re.compile(r"^[ \t]*env_file[ \t]*:", re.MULTILINE)

failures = []
warnings = []


def parse_env_example(path):
    """Return (keys, duplicate_keys) from a .env.example file."""
    seen = Counter()
    with open(path) as handle:
        for line in handle.read().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key = line.split("=", 1)[0].strip()
            if key:
                seen[key] += 1
    return set(seen), {k for k, n in seen.items() if n > 1}


def check_stack(stack, compose, example):
    with open(compose) as handle:
        compose_text = handle.read()

    all_refs = set(REF_RE.findall(compose_text))
    required = set()
    optional = set()
    for braced, modifier, bare in MOD_RE.findall(compose_text):
        name = braced or bare
        if modifier in OPTIONAL_MODS:
            optional.add(name)
        else:
            required.add(name)
    # A name is optional only if no reference to it requires a value.
    optional -= required
    required -= NOISE

    # Stacks using env_file: hand the whole .env to the container. Docker reads
    # those vars at runtime, so they never appear as ${VAR} in compose - the
    # dead-key check cannot apply.
    uses_env_file = bool(ENV_FILE_RE.search(compose_text))

    # A stack that references variables but ships no .env.example is a gap, not
    # a pass. The previous version did `continue` here, which repeats the
    # mistake has_changed() made in backup-services.sh: absence read as
    # "nothing to do". Silence is not success.
    if not os.path.exists(example):
        if required or uses_env_file:
            failures.append(stack)
            print(f"\n  FAIL  {stack}: references variables but has no .env.example")
            if uses_env_file:
                print("          uses env_file - the .env contents are undocumented")
            for key in sorted(required):
                print(f"          undocumented: {key}")
        else:
            print(f"    OK  {stack}  (no variables, no .env.example)")
        return

    example_keys, duplicates = parse_env_example(example)

    missing = required - example_keys
    # A key documenting an optional variable is still in use, so all_refs (not
    # required) belongs on the right side - as do $$-escaped names, which are
    # consumed inside the container.
    in_use = all_refs | set(ESCAPED_RE.findall(compose_text))
    dead = set() if uses_env_file else (example_keys - in_use)

    if missing or dead or duplicates:
        failures.append(stack)
        print(f"\n  FAIL  {stack}")
        for key in sorted(missing):
            print(f"          missing (in compose, not in .env.example): {key}")
        for key in sorted(dead):
            print(f"          dead (in .env.example, not in compose): {key}")
        for key in sorted(duplicates):
            # The later assignment silently wins. Nothing warns about it, and
            # the value you read in the file is not the one that is used.
            print(f"          duplicate declaration in .env.example: {key}")
    else:
        notes = []
        if uses_env_file:
            notes.append("env_file")
        if optional:
            notes.append(f"{len(optional)} with defaults")
        suffix = f"  [{', '.join(notes)}]" if notes else ""
        print(f"    OK  {stack}  ({len(example_keys)} keys){suffix}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        default=".",
        help="repository root (default: current directory)",
    )
    args = parser.parse_args()

    os.chdir(args.root)

    if not os.path.isdir(STACKS_ROOT):
        print(f"x {STACKS_ROOT} not found - is --root pointing at the repository root?")
        return 1

    found = 0
    # os.walk instead of os.listdir: the old version only looked one level
    # deep, so any nested stack directory was never checked and produced no
    # output at all - indistinguishable from "clean".
    for dirpath, dirnames, filenames in os.walk("."):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        if "docker-compose.yml" not in filenames and ".env.example" not in filenames:
            continue

        found += 1
        stack = os.path.relpath(dirpath, ".")
        compose = os.path.join(dirpath, "docker-compose.yml")
        example = os.path.join(dirpath, ".env.example")

        if not os.path.exists(compose):
            warnings.append(stack)
            print(f"  WARN  {stack}: .env.example exists but no docker-compose.yml")
            continue

        check_stack(stack, compose, example)

    print("")
    if not found:
        print("x no stacks found - check --root")
        return 1
    if warnings:
        print(f"{len(warnings)} warning(s): {', '.join(sorted(warnings))}")
    if failures:
        print(f"Drift detected in {len(failures)} stack(s): {', '.join(sorted(failures))}")
        return 1
    print(f"All {found} stacks clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
