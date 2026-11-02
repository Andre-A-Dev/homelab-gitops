#!/usr/bin/env python3
"""
mirror_check.py — deterministic checks for the public mirror checkout.

Run from the mirror checkout (the GitHub clone), never from the private repo.
Read-only: no git command here changes the index, refs or history, and nothing
talks to the network (a GitHub fetch would open a passphrase dialog).

    mirror_check.py preflight        remotes, upstream, tags, half-done rebases
    mirror_check.py scan             sync dry-run, leak scan (files, messages,
                                     tags), sanitizer artifacts
    mirror_check.py commits [--tree REF]
                                     unpushed commits: subject, files, modes

Private repo: $MIRROR_PRIVATE_REPO, default ~/Projects/gitea/homelab-infra.
The leak patterns are the left-hand side of REPLACEMENTS in the private
scripts/sync_public.py, imported at runtime, so no private value is written
into this public file.

Exit code: 1 if any FAIL was reported, else 0.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

PRIVATE = Path(os.environ.get("MIRROR_PRIVATE_REPO",
                              "~/Projects/gitea/homelab-infra")).expanduser()
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".zip", ".tar", ".gz",
              ".db", ".sqlite", ".bin", ".pdf", ".ttf", ".woff", ".woff2"}
# A placeholder followed by a prefix length only appears when the sanitizer
# rewrote a real CIDR, and the result no longer parses. Seen once with the
# Tailscale CGNAT range in the Caddyfile.
ARTIFACT_RE = re.compile(r"\b(\d+\.[xy]\.[xy]\.[xy]|\d+\.\d+\.\d+\.x)/\d+")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

failed = False


def say(level: str, msg: str) -> None:
    global failed
    if level == "FAIL":
        failed = True
    print(f"{level:5} {msg}")


def git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {res.stderr.strip()}")
    # Only trailing newlines: porcelain lines start with a significant space
    return res.stdout.rstrip("\n")


def mirror_root() -> Path:
    root = Path(git("rev-parse", "--show-toplevel")).resolve()
    if root == PRIVATE.resolve():
        sys.exit("Run this in the mirror checkout, not in the private repo.")
    return root


def upstream() -> str | None:
    res = subprocess.run(["git", "rev-parse", "--abbrev-ref", "@{u}"],
                         capture_output=True, text=True)
    return res.stdout.strip() if res.returncode == 0 else None


# ── preflight ────────────────────────────────────────────────────────────────

def preflight() -> None:
    root = mirror_root()
    private_urls = set(git("remote", "-v", cwd=PRIVATE).split()[1::3]) \
        if PRIVATE.is_dir() else set()

    remotes = {}
    for line in git("remote", "-v").splitlines():
        name, url, _ = line.split()
        remotes[name] = url
    if not remotes:
        say("FAIL", "no remote configured")
    for name, url in remotes.items():
        if url in private_urls:
            say("FAIL", f"remote '{name}' points to the private repo: {url}")
        elif "github.com" not in url:
            say("FAIL", f"remote '{name}' is not GitHub: {url}")
        else:
            say("OK", f"remote '{name}' → {url}")
    if "origin" in remotes:
        say("WARN", "remote is named 'origin': a 'remote set-url origin …' meant "
                    "for the private repo would silently retarget the mirror")

    up = upstream()
    if not up:
        say("FAIL", "main has no upstream")
    else:
        behind, ahead = git("rev-list", "--left-right", "--count",
                            "@{u}...HEAD").split()
        say("OK", f"upstream {up}: ahead {ahead}, behind {behind} (local refs, no fetch)")
        if behind != "0":
            say("WARN", "behind upstream: someone pushed elsewhere, or a fetch "
                        "came from the wrong remote")

    # Release tags sit on pushed mirror commits. A tag whose commit is not in
    # the upstream history came from the private repo (or was set before push).
    for tag in git("tag", "-l").split():
        commit = git("rev-parse", f"{tag}^{{commit}}")
        on_upstream = bool(up) and subprocess.run(
            ["git", "merge-base", "--is-ancestor", commit, "@{u}"],
            capture_output=True).returncode == 0
        if on_upstream:
            say("OK", f"tag {tag} on {commit[:7]} in {up}")
        else:
            say("FAIL", f"tag {tag} on {commit[:7]} is not in the upstream history: "
                        "from the private repo, or set before push (git tag -d …)")

    gitdir = Path(git("rev-parse", "--git-dir")).resolve()
    for marker in ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD"):
        if (gitdir / marker).exists():
            say("FAIL", f"operation in progress: {marker}")

    if PRIVATE.is_dir():
        private_head = git("rev-parse", "HEAD", cwd=PRIVATE)
        if subprocess.run(["git", "cat-file", "-e", private_head],
                          capture_output=True).returncode == 0:
            refs = git("for-each-ref", "--contains", private_head,
                       "--format=%(refname)")
            if refs:
                say("FAIL", f"private history reachable from: {refs.split()}")
            else:
                say("WARN", "private objects present but unreachable "
                            "(earlier wrong fetch); harmless, gc removes them")
        dirty = git("status", "--porcelain", "--untracked-files=no", cwd=PRIVATE)
        if dirty:
            say("WARN", "private repo has uncommitted changes to tracked files; "
                        "sync_public.py copies the working tree, so a sync now "
                        f"publishes them: {[l[3:] for l in dirty.splitlines()]}")
    else:
        say("WARN", f"private repo not found at {PRIVATE} (MIRROR_PRIVATE_REPO)")

    print(f"\nmirror: {root}\nprivate: {PRIVATE}")


# ── scan ─────────────────────────────────────────────────────────────────────

def load_patterns() -> list[re.Pattern]:
    sys.path.insert(0, str(PRIVATE / "scripts"))
    import sync_public  # noqa: E402  (private, not in the mirror)
    return [re.compile(p) for p, _ in sync_public.REPLACEMENTS]


def sync_dry_run(root: Path) -> None:
    python = PRIVATE / ".venv/bin/python"
    cmd = [str(python if python.exists() else "python3"),
           str(PRIVATE / "scripts/sync_public.py"), str(root), "--dry-run"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    out = ANSI_RE.sub("", res.stdout + res.stderr)
    counts = dict((k, int(v)) for v, k in
                  re.findall(r"^\s*(\d+)\s+(written|deleted|skipped)\b", out, re.M))
    if res.returncode != 0 or not counts:
        say("FAIL", f"sync dry-run failed (rc={res.returncode}):\n{out[-800:]}")
        return
    level = "OK" if counts.get("written", 0) == 0 and counts.get("deleted", 0) == 0 else "WARN"
    say(level, f"sync dry-run: {counts.get('written', 0)} written, "
               f"{counts.get('deleted', 0)} deleted, {counts.get('skipped', 0)} identical"
               + ("" if level == "OK" else " (mirror differs from the sanitized private tree)"))
    for line in out.splitlines():
        if re.match(r"\s*(NEW|WRITE|DELETE|WARNING)\b", line):
            print(f"      {line.strip()}")


def scan_files(root: Path, patterns: list[re.Pattern]) -> None:
    tracked = git("ls-files", "-z").split("\0")
    untracked = git("ls-files", "-z", "--others", "--exclude-standard").split("\0")
    hits = 0
    for rel in filter(None, tracked + untracked):
        path = root / rel
        if path.is_symlink():
            say("FAIL", f"{rel}: symlink (would publish its target)")
            continue
        if path.suffix.lower() in BINARY_EXT or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for i, pat in enumerate(patterns):
                if pat.search(line):
                    hits += 1
                    # Value masked: this output may end up in a public place
                    say("FAIL", f"{rel}:{n}: matches sanitizer rule #{i}")
            if ARTIFACT_RE.search(line):
                say("WARN", f"{rel}:{n}: placeholder with prefix length "
                            f"'{ARTIFACT_RE.search(line).group(0)}' — sanitizer "
                            "artifact, fix the rule in sync_public.py")
    if not hits:
        say("OK", f"leak scan: {len(patterns)} sanitizer rules, no hit in "
                  "tracked and untracked files")
    for rel in filter(None, untracked):
        say("INFO", f"untracked: {rel}")


def scan_messages(patterns: list[re.Pattern]) -> None:
    if not upstream():
        return
    log = git("log", "@{u}..HEAD", "--format=%h%x00%B%x01")
    private_hashes = set(git("rev-list", "--all", "--abbrev-commit",
                             "--abbrev=7", cwd=PRIVATE).split())
    for entry in filter(None, (e.strip() for e in log.split("\x01"))):
        sha, body = entry.split("\0", 1)
        for i, pat in enumerate(patterns):
            if pat.search(body):
                say("FAIL", f"commit {sha} message matches sanitizer rule #{i}")
        for word in re.findall(r"\b[0-9a-f]{7,40}\b", body):
            if word[:7] in private_hashes:
                say("WARN", f"commit {sha} message names private commit {word[:7]} "
                            "(not resolvable on GitHub)")
        if re.search(r"(?im)^co-authored-by:", body):
            say("WARN", f"commit {sha} message has a Co-Authored-By line")


def scan_tags(patterns: list[re.Pattern]) -> None:
    # Annotated tag messages are published with the tag
    refs = git("for-each-ref", "refs/tags", "--format=%(refname:short)%00%(contents)%01")
    for entry in filter(None, (e.strip() for e in refs.split("\x01"))):
        tag, body = entry.split("\0", 1)
        for i, pat in enumerate(patterns):
            if pat.search(body):
                say("FAIL", f"tag {tag} message matches sanitizer rule #{i}")


def scan() -> None:
    root = mirror_root()
    sync_dry_run(root)
    patterns = load_patterns()
    scan_files(root, patterns)
    scan_messages(patterns)
    scan_tags(patterns)


# ── commits ──────────────────────────────────────────────────────────────────

def commits(tree_ref: str | None) -> None:
    if not upstream():
        say("FAIL", "main has no upstream")
        return
    shas = git("rev-list", "--reverse", "@{u}..HEAD").split()
    if not shas:
        say("OK", "no unpushed commits")
    seen: dict[str, str] = {}
    for sha in shas:
        subject = git("log", "-1", "--format=%s", sha)
        short = sha[:7]
        files = git("show", "--format=", "--name-status", sha).splitlines()
        modes = [l.strip() for l in git("show", "--format=", "--summary", sha).splitlines()
                 if "mode" in l]
        print(f"{short} {subject}")
        for f in files:
            print(f"        {f}")
        for m in modes:
            print(f"        ({m})")
        if subject in seen:
            say("WARN", f"{short} repeats the subject of {seen[subject]}: "
                        "message copied onto the wrong content?")
        seen.setdefault(subject, short)
    authors = sorted(set(git("log", "@{u}..HEAD", "--format=%an <%ae>").splitlines()))
    if authors:
        print(f"authors: {authors}")
    if git("status", "--porcelain"):
        say("WARN", "working tree not clean: not everything is committed")
    if tree_ref:
        same = git("rev-parse", "HEAD^{tree}") == git("rev-parse", f"{tree_ref}^{{tree}}")
        say("OK" if same else "FAIL",
            f"HEAD tree {'identical to' if same else 'differs from'} {tree_ref}")


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = args[0], args[1:]
    if cmd == "preflight":
        preflight()
    elif cmd == "scan":
        scan()
    elif cmd == "commits":
        commits(rest[1] if rest[:1] == ["--tree"] and len(rest) > 1 else None)
    else:
        print(__doc__)
        return 2
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
