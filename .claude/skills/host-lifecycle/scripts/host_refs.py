#!/usr/bin/env python3
"""
host_refs.py — where a host is registered in this repo.

    host_refs.py refs <host> [--alias NAME ...] [--all]
        every file that names the host or one of its IPs
    host_refs.py gaps <reference> <new> [--alias NAME ...] [--all]
        files that name <reference> but not <new>: the registrations a new
        host still lacks, with the reference's lines as a template
    host_refs.py ips <host> [--alias NAME ...]
        the IPs found for the host and where they came from

Names match case-insensitively, also inside identifiers (hephaestus-ro,
proteus_host), not inside longer words. Use --alias for spellings the name
does not cover (AstraeusNX: --alias astraeus_nx). --alias applies to the
first host (refs, ips) or to <new> (gaps); the reference in gaps is matched
by its name and IPs only.

IPs come from shared/workstations/dotfiles/ssh_config (Host entries starting with the
name), shared/ssh/claude-ro.keys (from= of claude-ro@<name>) and the
Prometheus static targets labelled with the name. They also match in their
regex-escaped form, as the mirror tooling writes them.

The result is a candidate list, not a verdict: a hit can be unrelated (a
mouse model named Proteus), a gap can be intended (a workstation has no
apticron). Read-only, no network. Exit code 0.
"""

import argparse
import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[4]
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".pytest_cache"}
MIRROR_TOOLING = {"scripts/export_public.py", "scripts/sync_public.py",
                  "scripts/audit_repo.py", "scripts/publicignore.py",
                  "scripts/config/.publicignore"}
IP_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
LINES_PER_FILE = 5


def collect_ips(names):
    """IP -> source description, for any of the given names."""
    low = [n.lower() for n in names]
    found = {}

    ssh = REPO / "shared" / "workstations" / "dotfiles" / "ssh_config"
    if ssh.exists():
        hosts = []
        for line in ssh.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].lower() == "host":
                hosts = [h.lower() for h in parts[1:]]
            elif len(parts) >= 2 and parts[0].lower() == "hostname":
                if IP_RE.match(parts[1]) and any(h.startswith(n) for h in hosts for n in low):
                    found.setdefault(parts[1], f"ssh_config Host {' '.join(hosts)}")

    keys = REPO / "shared" / "ssh" / "claude-ro.keys"
    if keys.exists():
        for line in keys.read_text().splitlines():
            m = re.match(r'from="([\d.]+)/32"', line)
            if m and any(line.lower().rstrip().endswith(f"claude-ro@{n}") for n in low):
                found.setdefault(m.group(1), "claude-ro.keys from=")

    prom = REPO / "mnemosyne" / "stacks" / "monitoring" / "prometheus" / "prometheus.yml"
    if prom.exists():
        cfg = yaml.safe_load(prom.read_text()) or {}
        for job in cfg.get("scrape_configs") or []:
            for sc in job.get("static_configs") or []:
                labels = {str(v).lower() for v in (sc.get("labels") or {}).values()}
                if not labels & set(low):
                    continue
                for target in sc.get("targets") or []:
                    ip = str(target).rsplit(":", 1)[0]
                    if IP_RE.match(ip):
                        found.setdefault(ip, f"prometheus.yml job {job.get('job_name')}")
    return found


def patterns(names, ips):
    pats = [re.compile(rf"(?<![A-Za-z0-9]){re.escape(n)}(?![A-Za-z])", re.I) for n in names]
    for ip in ips:
        pats.append(re.compile(rf"(?<![\d.]){re.escape(ip)}(?!\d)"))
        pats.append(re.compile(re.escape(ip.replace(".", "\\."))))
    return pats


def files():
    me = Path(__file__).resolve()
    for p in sorted(REPO.rglob("*")):
        if p == me:
            continue  # the docstring names hosts as examples
        if p.is_file() and not SKIP_DIRS & set(p.relative_to(REPO).parts):
            try:
                yield p, p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue


def hits(pats):
    """relative path -> [(line number, line)] for every file with a match."""
    out = {}
    for path, text in files():
        lines = [(i, l) for i, l in enumerate(text.splitlines(), 1)
                 if any(p.search(l) for p in pats)]
        if lines:
            out[str(path.relative_to(REPO))] = lines
    return out


def show(result, show_all):
    for rel, lines in result.items():
        flag = "  [MIRROR TOOLING: flag every change]" if rel in MIRROR_TOOLING else ""
        print(f"\n## {rel}  ({len(lines)} line{'s' if len(lines) != 1 else ''}){flag}")
        shown = lines if show_all else lines[:LINES_PER_FILE]
        for i, l in shown:
            print(f"  {i}: {l.strip()[:160]}")
        if len(shown) < len(lines):
            print(f"  ... {len(lines) - len(shown)} more (--all)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("mode", choices=["refs", "gaps", "ips"])
    ap.add_argument("hosts", nargs="+")
    ap.add_argument("--alias", action="append", default=[])
    ap.add_argument("--all", action="store_true", help="every matching line, not just 5 per file")
    args = ap.parse_args()

    if args.mode == "gaps":
        if len(args.hosts) != 2:
            ap.error("gaps needs <reference> <new>")
        ref, new = args.hosts
        ref_ips = collect_ips([ref])
        new_names = [new] + args.alias
        new_ips = collect_ips(new_names)
        ref_hits = hits(patterns([ref], ref_ips))
        new_hits = hits(patterns(new_names, new_ips))
        gaps = {k: v for k, v in ref_hits.items() if k not in new_hits}
        print(f"{len(gaps)} file(s) name {ref} but not {new} "
              f"({ref} IPs: {', '.join(ref_ips) or 'none'}; {new} IPs: {', '.join(new_ips) or 'none'})")
        show(gaps, args.all)
        return 0

    if len(args.hosts) != 1:
        ap.error(f"{args.mode} takes one host")
    names = args.hosts + args.alias
    ips = collect_ips(names)
    if args.mode == "ips":
        for ip, src in ips.items():
            print(f"{ip}  {src}")
        if not ips:
            print("none")
        return 0
    result = hits(patterns(names, ips))
    print(f"{len(result)} file(s) name {'/'.join(names)} or IPs {', '.join(ips) or 'none'}")
    show(result, args.all)
    return 0


if __name__ == "__main__":
    sys.exit(main())
