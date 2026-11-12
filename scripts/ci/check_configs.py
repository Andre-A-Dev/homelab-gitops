#!/usr/bin/env python3
"""Structural validation for compose files, alert rules, Grafana dashboards
and the Mnemosyne Caddyfile.

Run locally before pushing:

    python3 scripts/ci/check_configs.py

Exit code 0 means every file is structurally sound. Warnings are printed but
never block; failures do.

This used to be three shell loops that embedded the filename directly into a
Python one-liner ("yaml.safe_load(open('$f'))"). Any path containing a quote or
a space produced either a syntax error or a silent skip, and `for f in $(find
...)` word-split before Python ever saw the name. Walking the tree in Python
removes that whole failure class — and makes the check runnable on AstraeusNX
instead of only inside the runner.
"""

import argparse
import json
import os
import re
import subprocess
import sys

import yaml

STACKS = "mnemosyne/stacks"
MONITORING = "mnemosyne/stacks/monitoring"
DASHBOARDS = "mnemosyne/stacks/monitoring/grafana/dashboards"
CADDYFILE = "mnemosyne/stacks/caddy/Caddyfile"
PROFILES_FILE = "shared/compose-profiles.yml"
# Exists only in the private repo; its absence marks a public-mirror checkout,
# where the paths it lists are missing on purpose.
PUBLICIGNORE = "scripts/config/.publicignore"
PURPOSES = {"switched-off", "on-demand"}  # see the header of PROFILES_FILE

# Update class per service (wiki: Update-Strategy). Read back by
# check-container-updates.sh, so a missing or wrong label would silently drop
# a service from its class hints. C is exactly the services with `build:`.
UPDATE_CLASS_LABEL = "homelab.update-class"
UPDATE_CLASSES = {
    "A": "stateful, schema migrations - major/minor pin, backup before upgrade",
    "B": "stateless infra and exporters - major tag",
    "C": "local build - base image refreshed with build --pull",
}

failures = []
warnings = []


def fail(path, msg):
    failures.append(f"{path}: {msg}")
    print(f"  x {path}: {msg}")


def warn(path, msg):
    warnings.append(f"{path}: {msg}")
    print(f"  ! {path}: {msg}")


def ok(path, note=""):
    print(f"  + {path}{'  ' + note if note else ''}")


def walk(root, predicate):
    """Yield files under root matching predicate, skipping .git."""
    if not os.path.isdir(root):
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            if predicate(name, path):
                yield path


def load_profiles():
    """Return the set of known profile names, or None if the file is broken.

    The vocabulary (section `profiles`) is the single place where a profile
    name gets its meaning; the state (section `hosts`) says which profiles are
    switched on where. Without the file every name would be valid, typos
    included, and the drift report could no longer tell "switched off" from
    "on demand".
    """
    print("\n== compose-profiles.yml ==")
    try:
        with open(PROFILES_FILE) as handle:
            data = yaml.safe_load(handle)
    except FileNotFoundError:
        fail(PROFILES_FILE, "missing - every compose profile must be declared there")
        return None
    except yaml.YAMLError as exc:
        fail(PROFILES_FILE, f"invalid YAML - {exc}")
        return None

    if not isinstance(data, dict) or not isinstance(data.get("profiles"), dict) or not data["profiles"]:
        fail(PROFILES_FILE, "no 'profiles' mapping")
        return None
    profiles = data["profiles"]
    hosts = data.get("hosts")

    broken = False
    for name, spec in profiles.items():
        # PyYAML (YAML 1.1) turns on/off/yes/no/true/false into booleans. A
        # profile named "off" would arrive here as False - and never match the
        # string Compose uses.
        if not isinstance(name, str):
            fail(PROFILES_FILE, f"profile name {name!r} is not a string - "
                                "on/off/yes/no/true/false are reserved in YAML")
            broken = True
            continue
        if not isinstance(spec, dict) or spec.get("purpose") not in PURPOSES:
            fail(PROFILES_FILE, f"profile '{name}': purpose must be one of {sorted(PURPOSES)}")
            broken = True

    if not isinstance(hosts, dict):
        fail(PROFILES_FILE, "no 'hosts' mapping")
        broken = True
    else:
        for host, active in hosts.items():
            if not isinstance(active, list):
                fail(PROFILES_FILE, f"hosts.{host} must be a list of profile names")
                broken = True
                continue
            for profile in active:
                if profile not in profiles:
                    fail(PROFILES_FILE, f"hosts.{host}: unknown profile {profile!r}")
                    broken = True
            if active:
                # Stage 1: nothing switches profiles yet. Accepting entries
                # would let the file claim a state that deploy and drift check
                # never apply - the gap between intent and reality this repo
                # keeps hitting.
                fail(PROFILES_FILE, f"hosts.{host}: must be empty - "
                                    "profile switching is not implemented yet")
                broken = True

    if not broken:
        ok(PROFILES_FILE, f"({len(profiles)} profiles: {', '.join(sorted(profiles))})")
    return {name for name in profiles if isinstance(name, str)}


def label_problems(service):
    """Return what is wrong with a service's update-class and Diun labels."""
    labels = service.get("labels") or {}
    if isinstance(labels, list):
        # List form "key=value" is valid compose syntax too.
        labels = dict(item.split("=", 1) if "=" in item else (item, "") for item in labels)
    value = labels.get(UPDATE_CLASS_LABEL)
    if value is None:
        return [f"missing label {UPDATE_CLASS_LABEL} ({'/'.join(UPDATE_CLASSES)}, see wiki Update-Strategy)"]
    if value not in UPDATE_CLASSES:
        return [f"{UPDATE_CLASS_LABEL} '{value}' is not one of {sorted(UPDATE_CLASSES)}"]
    if ("build" in service) != (value == "C"):
        return [f"{UPDATE_CLASS_LABEL} '{value}' does not match - "
                "class C is exactly the services with `build:`"]

    problems = []
    # A local build is in no registry; Diun logs a warning for it on every run.
    if value == "C" and str(labels.get("diun.enable")).lower() != "false":
        problems.append('class C needs diun.enable: "false" (local build, not in any registry)')

    # watch_repo without a filter queries every tag of the repository (Docker
    # Hub rate limits) and reports rebuilds of old versions. The filter must at
    # least match the pinned tag, which catches typos and a bound left behind.
    if str(labels.get("diun.watch_repo")).lower() == "true":
        include = labels.get("diun.include_tags")
        image = str(service.get("image", ""))
        if not include:
            problems.append("diun.watch_repo needs diun.include_tags")
        elif "${" not in image:
            tag = image.rsplit(":", 1)[1] if ":" in image.rsplit("/", 1)[-1] else "latest"
            patterns = include.replace("$$", "$").split(";")  # $$ is Compose's escape
            try:
                if not any(re.search(rx, tag) for rx in patterns):
                    problems.append(f"diun.include_tags does not match the pinned tag '{tag}'")
            except re.error as exc:
                problems.append(f"diun.include_tags is not a valid regex - {exc}")
    return problems


def bind_sources(compose_path, service):
    """Return (checked, missing) for the service's relative bind-mount sources.

    `missing` holds (source, error) pairs; error is empty when git answered
    "not ignored" and the git message when it could not answer at all.

    Docker creates a missing bind source as an empty root-owned directory and
    starts the container anyway: a config file that never reached the host
    turns into a directory nobody can delete without sudo, and the service
    runs without its config. A missing source is fine only if it is
    gitignored - that declares it host-local (runtime data, secrets), created
    on the target host and never part of a checkout.
    """
    base = os.path.dirname(compose_path)
    checked = 0
    missing = []
    for volume in service.get("volumes") or []:
        if isinstance(volume, str):
            source = volume.split(":", 1)[0]
        elif isinstance(volume, dict) and volume.get("type") == "bind":
            source = str(volume.get("source", ""))
        else:
            continue
        # Absolute sources are host paths, named volumes have no leading dot.
        if not source.startswith("."):
            continue
        checked += 1
        path = os.path.normpath(os.path.join(base, source))
        if os.path.exists(path):
            continue
        # The source type is unknown here, so ask for both forms: a pattern
        # like `data/` only matches the path with a trailing slash.
        result = subprocess.run(["git", "check-ignore", path, path + "/"],
                                capture_output=True, text=True)
        if result.returncode == 1:
            missing.append((source, ""))
        elif result.returncode != 0:
            # 128: not a git checkout. Can't tell, so it is not "ignored".
            missing.append((source, result.stderr.strip() or f"exit {result.returncode}"))
    return checked, missing


def check_compose(known_profiles):
    print("\n== docker-compose.yml ==")
    count = 0
    binds = 0
    used = set()
    mirror = not os.path.isfile(PUBLICIGNORE)
    # Whole repo, not only mnemosyne/stacks: the Zephyros fritz-exporter
    # crash loop (2026-10-14) came from a compose file this check never saw.
    for path in walk(".", lambda n, p: n == "docker-compose.yml"):
        count += 1
        try:
            with open(path) as handle:
                data = yaml.safe_load(handle)
        except yaml.YAMLError as exc:
            fail(path, f"invalid YAML - {exc}")
            continue

        # A file that parses but has no services key is valid YAML and a broken
        # stack. Parsing alone never caught that.
        if not isinstance(data, dict):
            fail(path, "top level is not a mapping")
        elif "services" not in data:
            fail(path, "no top-level 'services' key")
        elif not data["services"]:
            fail(path, "'services' is empty")
        else:
            bad = []
            for name, service in data["services"].items():
                for problem in label_problems(service or {}):
                    bad.append(f"service '{name}': {problem}")
                for profile in (service or {}).get("profiles") or []:
                    if not isinstance(profile, str):
                        bad.append(f"service '{name}': profile {profile!r} is not a string - "
                                   "quote it, or avoid on/off/yes/no")
                        continue
                    used.add(profile)
                    if known_profiles is not None and profile not in known_profiles:
                        bad.append(f"service '{name}': profile '{profile}' not in {PROFILES_FILE}")
                checked, missing = bind_sources(path, service or {})
                binds += checked
                for source, error in missing:
                    msg = f"service '{name}': bind source '{source}' does not exist and is not gitignored"
                    if error:
                        bad.append(f"service '{name}': bind source '{source}' does not exist, "
                                   f"git check-ignore failed - {error}")
                    elif mirror:
                        # The private CI sees the same commit and blocks the
                        # deploy; here the source may just be a private path.
                        warn(path, f"{msg} - excluded from the public mirror?")
                    else:
                        bad.append(f"{msg} - Docker would create it as an empty root-owned directory")
            if bad:
                for problem in bad:
                    fail(path, problem)
            else:
                ok(path, f"({len(data['services'])} services)")
    if known_profiles:
        for profile in sorted(known_profiles - used):
            # Not an error: a profile may be declared before its first use
            # (debug), or stay after the last service left it.
            warn(PROFILES_FILE, f"profile '{profile}' is declared but not used by any service")
    print(f"  {count} compose file(s) checked, {binds} relative bind source(s)")
    return count


def is_rule_file(name, path):
    if not name.endswith((".yml", ".yaml")):
        return False
    try:
        with open(path) as handle:
            return any(line.startswith("groups:") for line in handle)
    except OSError:
        return False


def check_alert_rules():
    print("\n== Prometheus rule files ==")
    count = 0
    for path in walk(MONITORING, is_rule_file):
        count += 1
        try:
            with open(path) as handle:
                data = yaml.safe_load(handle)
        except yaml.YAMLError as exc:
            fail(path, f"invalid YAML - {exc}")
            continue

        problems = []
        rule_count = 0
        for group in data.get("groups") or []:
            if "name" not in group:
                problems.append("group without name")
                continue
            if "rules" not in group:
                problems.append(f"group '{group['name']}' has no rules")
                continue
            for rule in group["rules"]:
                rule_count += 1
                label = rule.get("alert") or rule.get("record") or "<unnamed>"
                # An unnamed rule, or one without expr, is rejected by Prometheus
                # at reload time - i.e. long after the commit that caused it.
                if "alert" not in rule and "record" not in rule:
                    problems.append(f"rule without alert/record in '{group['name']}'")
                if "expr" not in rule:
                    problems.append(f"'{label}' has no expr")
                # Alert routing is by severity label (ntfy topics
                # Mnemosyne-Critical-* / Mnemosyne-Warning-*). An alert without
                # severity does not fail - it silently lands in the default
                # route and is never seen on the phone. Promote this to fail()
                # once the existing rule set is clean.
                if "alert" in rule and not (rule.get("labels") or {}).get("severity"):
                    warn(path, f"alert '{label}' has no severity label")

        if problems:
            for problem in problems:
                fail(path, problem)
        else:
            ok(path, f"({rule_count} rules)")
    print(f"  {count} rule file(s) checked")
    return count


def check_dashboards():
    print("\n== Grafana dashboards ==")
    count = 0
    uids = {}
    for path in walk(DASHBOARDS, lambda n, p: n.endswith(".json")):
        count += 1
        try:
            with open(path) as handle:
                data = json.load(handle)
        except json.JSONDecodeError as exc:
            fail(path, f"invalid JSON - {exc}")
            continue

        uid = data.get("uid")
        if not uid:
            # Without a uid Grafana generates one on every provisioning run, so
            # dashboard links and alert annotations break after each recreate.
            #
            # Warning, not failure: this is untidy, not broken. Four dashboards
            # have shipped without a uid for months and Grafana renders them
            # fine. Blocking on it meant that on 2026-08-30 the whole validate
            # job aborted here and never reached the executable-bit gate three
            # steps later — a commit with a 100644 shell script went through
            # unchecked because an unrelated cosmetic issue failed first.
            # Promote back to fail() once the existing dashboards are clean.
            warn(path, "no 'uid' - provisioning assigns a new one on every restart")
        elif uid in uids:
            # Two files with the same uid: the second silently overwrites the
            # first. Nothing logs an error, the dashboard is just gone.
            fail(path, f"duplicate uid '{uid}', already used by {uids[uid]}")
        else:
            uids[uid] = path
            ok(path, f"(uid {uid})")
    print(f"  {count} dashboard(s) checked")
    return count


def check_caddyfile():
    """Every .home site on Mnemosyne must import the lan_only snippet.

    Port 443 is forwarded from the internet for the public sites and Caddy
    picks the site by Host, so a .home block without the snippet answers
    anyone who sends its name to the public IP. Nothing else stops that.
    """
    print("\n== Caddyfile ==")
    try:
        with open(CADDYFILE) as handle:
            lines = handle.readlines()
    except FileNotFoundError:
        fail(CADDYFILE, "missing")
        return 0

    depth = 0
    site = None  # (line number, addresses) of the open top-level block
    imports = set()
    sites = 0
    missing = 0
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if depth == 0 and line.endswith("{"):
            site = (number, line[:-1].strip())
            imports = set()
        elif depth == 1 and line.startswith("import "):
            imports.add(line.split()[1])
        depth += line.count("{") - line.count("}")
        if depth < 0:
            fail(CADDYFILE, f"line {number}: unbalanced '}}'")
            return 1
        if depth == 0 and site:
            start, addresses = site
            site = None
            # Snippets "(name)" and the global options block "{" are no sites.
            if not addresses or addresses.startswith("("):
                continue
            hosts = [a.split("://")[-1].rsplit(":", 1)[0] for a in re.split(r"[,\s]+", addresses) if a]
            if not any(h.endswith(".home") for h in hosts):
                continue
            sites += 1
            if "lan_only" not in imports:
                missing += 1
                fail(CADDYFILE, f"line {start}: '{addresses}' has no 'import lan_only'")
    if depth != 0:
        fail(CADDYFILE, "unbalanced braces at end of file")
    elif sites == 0:
        # Silence is not success: a parser that finds nothing would pass.
        fail(CADDYFILE, "no .home site found - verify the parser in this script")
    elif not missing:
        ok(CADDYFILE, f"({sites} .home sites, all import lan_only)")
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        default=".",
        help="repository root (default: current directory)",
    )
    args = parser.parse_args()

    os.chdir(args.root)

    if not os.path.isdir(STACKS):
        print(f"x {STACKS} not found - is --root pointing at the repository root?")
        return 1

    total = (check_compose(load_profiles()) + check_alert_rules() + check_dashboards()
             + check_caddyfile())

    print("")
    if not total:
        # No files found is not the same as no problems found. Silence is not
        # success - a wrong path would otherwise report a clean run.
        print("x no files checked at all - verify the paths in this script")
        return 1
    if warnings:
        print(f"{len(warnings)} warning(s) - not blocking:")
        for item in warnings:
            print(f"    {item}")
    if failures:
        print(f"\n{len(failures)} failure(s):")
        for item in failures:
            print(f"    {item}")
        return 1
    print(f"All {total} config file(s) valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
