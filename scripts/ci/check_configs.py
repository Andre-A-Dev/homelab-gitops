#!/usr/bin/env python3
"""Structural validation for compose files, alert rules and Grafana dashboards.

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
import sys

import yaml

STACKS = "mnemosyne/stacks"
MONITORING = "mnemosyne/stacks/monitoring"
DASHBOARDS = "mnemosyne/stacks/monitoring/grafana/dashboards"
PROFILES_FILE = "shared/compose-profiles.yml"
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


def check_compose(known_profiles):
    print("\n== docker-compose.yml ==")
    count = 0
    used = set()
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
    print(f"  {count} compose file(s) checked")
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

    total = check_compose(load_profiles()) + check_alert_rules() + check_dashboards()

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
