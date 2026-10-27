#!/usr/bin/env python3
"""Nightly drift check for Mnemosyne - REPORT ONLY, never fixes anything.

Compares what should run (Git) with what does run (Docker) and reports every
difference to Healthchecks.io. Drift always means something bypassed the
deploy path - a human should look at it, not a script silently overwrite it.

Why this exists: from 2026-06-16 (f4b0322) to 2026-10-13 every push to
Mnemosyne was pull-only and nothing noticed. Any one of the checks below
would have reported it the next morning.

Checks:
  repo     origin/main has commits HEAD does not  -> deploy path broken
           HEAD has commits origin/main does not  -> commits made on the server
           tracked files modified / stray files   -> edited on the server
  stacks   `docker compose up -d --dry-run` would create or recreate a
           container -> running state differs from compose + .env + image.
           Each finding names the cause (config hash, image ID or missing
           container), the last commit on the stack and the fix command.
  mounts   a repo-managed file bind-mounted as a single file is newer than
           its container -> container still sees the old inode
  containers  a container exists for an inactive or removed service, or an
           active service's container is stopped or crash-looping
  tailscale   the subnet routes this node serves differ from EXPECTED_ROUTES
           -> .home unreachable from outside the LAN

Exit codes (also sent to Healthchecks as /<code>):
  0  no drift
  1  drift found
  2  a check itself failed - result incomplete

Without HC_DRIFT in the environment the report is printed only; useful for a
manual run. The systemd unit refuses to start without it.
"""

import datetime
import fcntl
import glob
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request

# Same directory, standard library only: the one list of stacks the deploy
# handler never touches, so the report can tell "manual step missing" from
# "deploy broken".
from deploy_plan import MANUAL_STACKS

REPO = "/home/andre/homelab-infra"
STACKS_DIR = "/home/andre/stacks"
# Shared with deploy-handler.sh: never inspect a half-applied deploy.
LOCK = "/run/lock/mnemosyne-deploy.lock"
LOCK_TIMEOUT = 900
# Vocabulary of profile names and their purpose (switched-off / on-demand).
PROFILES_FILE = os.path.join(REPO, "shared", "compose-profiles.yml")
HC_URL = "https://hc-ping.com/"
# A file this much newer than its container's start counts as stale. Covers a
# checkout that lands while the container is still starting.
STALE_SLACK = 60

# Compose (5.6) prints "Recreate" for a changed container but "Creating" for a
# missing one - the old "(Recreate|Create)$" never matched the latter, so a
# container removed by hand went unreported.
DRY_RUN_RE = re.compile(r"Container (\S+)\s+(Recreate|Creating)$")

# Subnet routes Mnemosyne must serve in the tailnet: Boreas (DNS, target of
# the split-DNS route for .home) and Mnemosyne itself (Caddy). Single hosts,
# not the /24 - the Remote uses the same subnet. The previous route vanished
# unnoticed at some point before 2026-10-21, and .home was unreachable on the
# road until someone tried. Advertising is set with `tailscale set`, approval
# happens in the admin console - either one going missing shows up here.
EXPECTED_ROUTES = {"192.168.1.11/32", "192.168.1.10/32"}


def run(cmd, cwd=None, env=None):
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)


def indent(text):
    return "\n".join("    " + line for line in text.splitlines())


def check_repo(findings, errors):
    result = run(["git", "fetch", "--quiet", "origin", "main"], cwd=REPO)
    if result.returncode:
        errors.append(f"repo: git fetch failed: {result.stderr.strip()}")
        return

    behind = run(["git", "rev-list", "--count", "HEAD..origin/main"], cwd=REPO).stdout.strip()
    ahead = run(["git", "rev-list", "--count", "origin/main..HEAD"], cwd=REPO).stdout.strip()
    head = run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO).stdout.strip()

    if behind not in ("", "0"):
        findings.append(
            f"repo: {behind} commit(s) on origin/main not deployed (HEAD {head}) "
            "- deploy job failing, runner down, or validate red"
        )
    if ahead not in ("", "0"):
        findings.append(f"repo: {ahead} local commit(s) not on origin/main - committed on the server?")

    status = run(["git", "status", "--porcelain"], cwd=REPO).stdout.rstrip()
    if status:
        findings.append("repo: working tree differs from HEAD - edited on the server?\n" + indent(status))


def last_change(stack_dir):
    """The last commit that touched this stack, as context for a finding."""
    result = run(["git", "log", "-1", "--format=%h %ad %s", "--date=short", "--", "."], cwd=stack_dir)
    return result.stdout.strip() or "no commit found"


def explain_dry_run(stack, stack_dir, env, container, action):
    """Why Compose would create/recreate a container, and the command to fix it.

    "dry-run would X recreate" alone left the reader to work out the cause.
    Compose decides from the config hash label and the image ID, so the same
    two comparisons name the cause.
    """
    manual = stack in MANUAL_STACKS
    by_hand = f"cd ~/stacks/{stack} && docker compose up -d"
    if action == "create":
        return ("container does not exist - never started, or removed by hand", by_hand)

    fmt = ('{{index .Config.Labels "com.docker.compose.service"}}\t'
           '{{index .Config.Labels "com.docker.compose.config-hash"}}\t{{.Image}}\t{{.Config.Image}}')
    fields = run(["docker", "inspect", "--format", fmt, container]).stdout.strip().split("\t")
    service, running_hash, running_image, image_ref = (fields + ["", "", "", ""])[:4]
    hashes = dict(line.split(None, 1) for line in
                  run(["docker", "compose", "config", "--hash", "*"], cwd=stack_dir, env=env).stdout.splitlines()
                  if " " in line)

    if service in hashes and hashes[service].strip() != running_hash:
        why = "definition changed (docker-compose.yml or .env) since the container was created"
        fix = (f"never applied by the deploy handler - {by_hand}" if manual else
               f"the deploy should have applied it - check `journalctl -u webhook | grep result=`; by hand: {by_hand}")
        return why, fix

    local_image = run(["docker", "image", "inspect", "--format", "{{.Id}}", image_ref]).stdout.strip()
    if local_image and local_image != running_image:
        return (f"a newer {image_ref} was pulled but not applied",
                f"{by_hand} {service}  (class A: sudo verify-backup.sh first)")
    return ("Compose sees a difference it does not name (network, dependency, volume)",
            f"cd ~/stacks/{stack} && docker compose up -d --dry-run   # shows what changes")


def compose_services(stack_dir, env):
    """Service names Compose would run with the default (no) profiles."""
    result = run(["docker", "compose", "config", "--services"], cwd=stack_dir, env=env)
    if result.returncode:
        return None
    return set(result.stdout.split())


def profiled_services(stack_dir, env):
    """{service: [profiles]} for every service behind a profile.

    On failure returns Compose's error text (str) instead - with all profiles
    enabled, Compose also resolves services that normally stay unloaded, e.g.
    an env_file that does not exist yet for an inactive stack.

    The profile name is shown in the report: "seasonal" or "inactive" means
    switched off, "cli" means an on-demand job (jobiris-monitor via
    `docker compose run`). Hardcoding which names count as "off" would go
    stale with the next profile name.
    """
    result = run(["docker", "compose", "--profile", "*", "config", "--format", "json"], cwd=stack_dir, env=env)
    if result.returncode:
        return result.stderr.strip() or "no error output"
    services = json.loads(result.stdout).get("services") or {}
    return {name: svc["profiles"] for name, svc in services.items() if svc.get("profiles")}


def check_containers(stack, stack_dir, env, active, defined, findings, errors):
    """Compare existing containers of the project with the active services.

    Catches what a dry-run cannot see:
      - a container whose service is inactive (profile) or no longer defined
        (orphan) - e.g. a profile set but the running container never removed,
        or a second compose directory sharing the project name (Zephyros,
        2026-10-14)
      - an active service whose container is stopped or crash-looping
        (`docker compose stop` forgotten; fusionsolar-exporter restarting
        unnoticed in an inactive stack, 2026-10-15)
    """
    result = run(["docker", "compose", "config", "--format", "json"], cwd=stack_dir, env=env)
    if result.returncode:
        errors.append(f"{stack}: docker compose config failed\n{indent(result.stderr.strip())}")
        return
    project = json.loads(result.stdout).get("name", stack)

    fmt = '{{.Label "com.docker.compose.service"}}\t{{.Names}}\t{{.State}}\t{{.Label "com.docker.compose.oneoff"}}'
    result = run(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={project}", "--format", fmt])
    if result.returncode:
        errors.append(f"{stack}: docker ps failed: {result.stderr.strip()}")
        return

    for line in result.stdout.splitlines():
        service, name, state, oneoff = (line.split("\t") + ["", "", "", ""])[:4]
        if oneoff == "True":
            continue  # `docker compose run` jobs (jobiris-monitor) come and go
        if service not in active and defined and service in defined:
            findings.append(f"{stack}: {name} exists ({state}) but service '{service}' is behind a profile "
                            f"- cd ~/stacks/{stack} && docker compose --profile '*' rm -sf {service}")
        elif service not in active:
            findings.append(f"{stack}: {name} exists ({state}) but service '{service}' is not defined here "
                            "- orphan, or a second compose dir with the same project name")
        elif state in ("exited", "dead", "created", "restarting"):
            findings.append(f"{stack}: {name} is {state} although service '{service}' is active\n"
                            f"    look: docker logs --tail 50 {name}\n"
                            f"    fix:  cd ~/stacks/{stack} && docker compose up -d {service}")


def check_stacks(findings, errors, inactive):
    composes = sorted(glob.glob(os.path.join(STACKS_DIR, "*", "docker-compose.yml")))
    if not composes:
        # No stacks found is not "no drift" - a wrong path would report clean.
        errors.append(f"stacks: no docker-compose.yml found under {STACKS_DIR}")
        return

    for compose in composes:
        stack_dir = os.path.dirname(compose)
        stack = os.path.basename(stack_dir)
        # PWD must be set explicitly. ~/stacks is a symlink into the repo; with
        # cwd= alone the child inherits the caller's PWD, Go's os.Getwd() falls
        # back to the resolved path, Compose sees the project under
        # ~/homelab-infra/... and every relative bind mount gets a different
        # config hash -> false "recreate" for every stack using ./ mounts.
        # A shell `cd` sets PWD, which is why manual dry-runs never showed it.
        env = dict(os.environ, PWD=stack_dir)

        active = compose_services(stack_dir, env)
        if active is None:
            errors.append(f"{stack}: docker compose config --services failed")
            continue

        # Services behind a profile, listed every morning with their profile
        # so deliberately switched-off ones are not forgotten.
        profiled = profiled_services(stack_dir, env)
        if isinstance(profiled, str):
            errors.append(f"{stack}: docker compose --profile '*' config failed\n{indent(profiled)}")
            profiled = {}
        if not active:
            inactive.append((stack, sorted({p for profs in profiled.values() for p in profs})))
        else:
            inactive += [(f"{stack}/{svc}", list(profs)) for svc, profs in sorted(profiled.items())]
        defined = active | set(profiled)

        check_containers(stack, stack_dir, env, active, defined, findings, errors)
        if not active:
            continue  # nothing Compose would start - a dry-run only errors out

        result = run(["docker", "compose", "up", "-d", "--dry-run"], cwd=stack_dir, env=env)
        output = (result.stdout + result.stderr).strip()
        if result.returncode:
            errors.append(f"{stack}: docker compose dry-run failed\n{indent(output)}")
            continue
        actions = set()
        for line in output.splitlines():
            match = DRY_RUN_RE.search(line.strip())
            if match:
                actions.add((match.group(1), "recreate" if match.group(2) == "Recreate" else "create"))
        if actions:
            last = last_change(stack_dir)
        for container, action in sorted(actions):
            why, fix = explain_dry_run(stack, stack_dir, env, container, action)
            findings.append(f"{stack}: {container} would be {action}d\n"
                            f"    why:  {why}\n"
                            f"    last: {last}\n"
                            f"    fix:  {fix}")


def started_epoch(started_at):
    # Docker reports nanoseconds, which datetime cannot parse - seconds suffice.
    stamp = datetime.datetime.strptime(started_at[:19], "%Y-%m-%dT%H:%M:%S")
    return stamp.replace(tzinfo=datetime.timezone.utc).timestamp()


def check_stale_mounts(findings, errors):
    ids = run(["docker", "ps", "-q"]).stdout.split()
    if not ids:
        errors.append("mounts: no running containers - is Docker up?")
        return
    result = run(["docker", "inspect", *ids])
    if result.returncode:
        errors.append(f"mounts: docker inspect failed: {result.stderr.strip()}")
        return

    repo_root = os.path.realpath(REPO)
    for container in json.loads(result.stdout):
        name = container["Name"].lstrip("/")
        started = started_epoch(container["State"]["StartedAt"])
        for mount in container.get("Mounts") or []:
            if mount.get("Type") != "bind":
                continue
            source = os.path.realpath(mount["Source"])
            # Only repo-managed single files: directory mounts see changes
            # live, and host files like /usr/bin/docker change with apt.
            if not source.startswith(repo_root + os.sep) or not os.path.isfile(source):
                continue
            changed = os.path.getmtime(source)
            if changed > started + STALE_SLACK:
                rel = os.path.relpath(source, repo_root)
                age = datetime.datetime.fromtimestamp(changed).strftime("%Y-%m-%d %H:%M")
                labels = container.get("Config", {}).get("Labels") or {}
                workdir = labels.get("com.docker.compose.project.working_dir", "<stack dir>")
                service = labels.get("com.docker.compose.service", "<service>")
                findings.append(f"{name}: {rel} changed {age}, container started before - old inode\n"
                                f"    why:  the deploy handler recreates on such changes - was it a manual edit or a failed deploy?\n"
                                f"    fix:  cd {workdir} && docker compose up -d --force-recreate {service}")


def check_tailscale(findings, errors):
    # PrimaryRoutes, not the advertised prefs: a route only counts once it is
    # advertised, approved in the admin console and active on this node.
    result = run(["tailscale", "status", "--json"])
    if result.returncode:
        errors.append(f"tailscale: status failed: {result.stderr.strip()}")
        return
    served = set(json.loads(result.stdout).get("Self", {}).get("PrimaryRoutes") or [])

    missing = EXPECTED_ROUTES - served
    unexpected = served - EXPECTED_ROUTES
    if missing:
        findings.append(f"tailscale: route(s) {', '.join(sorted(missing))} not served "
                        "- check `tailscale debug prefs` (AdvertiseRoutes) and approval in the admin console")
    if unexpected:
        findings.append(f"tailscale: unexpected route(s) {', '.join(sorted(unexpected))} served")


def load_purposes(errors):
    """{profile: purpose} from compose-profiles.yml, or {} if unreadable."""
    try:
        import yaml  # PyYAML (python3-yaml) - imported here so the rest runs without it
    except ImportError:
        errors.append("profiles: PyYAML missing - sudo apt install python3-yaml")
        return {}
    try:
        with open(PROFILES_FILE) as handle:
            profiles = (yaml.safe_load(handle) or {}).get("profiles") or {}
        return {name: (spec or {}).get("purpose", "?") for name, spec in profiles.items()}
    except (OSError, yaml.YAMLError, AttributeError) as exc:
        errors.append(f"profiles: cannot read {PROFILES_FILE}: {exc}")
        return {}


def profile_sections(entries, purposes, findings):
    """Group (label, profiles) by purpose. Unknown profile names are drift:
    CI rejects them, so one on the host means CI was bypassed."""
    sections = {"switched-off": [], "on-demand": []}
    for label, profiles in entries:
        shown = f"{label} [{','.join(profiles)}]" if profiles else label
        unknown = [p for p in profiles if p not in purposes]
        if purposes and unknown:
            findings.append(f"{label}: profile {','.join(unknown)} not declared in compose-profiles.yml")
        kinds = {purposes.get(p) for p in profiles}
        key = "on-demand" if "on-demand" in kinds else "switched-off"
        sections[key].append(shown)
    return sections


def acquire_lock():
    handle = open(LOCK, "a")
    deadline = time.time() + LOCK_TIMEOUT
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError:
            if time.time() > deadline:
                return None
            time.sleep(10)


def ping(uuid, suffix, body=""):
    try:
        request = urllib.request.Request(HC_URL + uuid + suffix, data=body.encode()[:100_000])
        urllib.request.urlopen(request, timeout=15)
    except OSError as exc:
        # Not fatal: a missing ping makes Healthchecks alert on its own.
        print(f"! healthchecks ping {suffix or '/'} failed: {exc}", file=sys.stderr)


def main():
    uuid = os.environ.get("HC_DRIFT", "").strip()
    if uuid:
        ping(uuid, "/start")

    findings, errors, inactive = [], [], []
    lock = acquire_lock()
    if lock is None:
        errors.append(f"lock: {LOCK} held for over {LOCK_TIMEOUT}s - deploy stuck?")
    else:
        checks = (
            lambda: check_repo(findings, errors),
            lambda: check_stacks(findings, errors, inactive),
            lambda: check_stale_mounts(findings, errors),
            lambda: check_tailscale(findings, errors),
        )
        for name, check in zip(("check_repo", "check_stacks", "check_stale_mounts", "check_tailscale"), checks):
            try:
                check()
            except Exception as exc:  # noqa: BLE001 - one broken check must not hide the others
                errors.append(f"{name}: crashed: {exc!r}")
        lock.close()

    sections = profile_sections(inactive, load_purposes(errors), findings) if inactive else {}

    code = 2 if errors else (1 if findings else 0)
    header = (
        f"drift-check {socket.gethostname()} "
        f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}: "
        f"{len(findings)} finding(s), {len(errors)} error(s)"
    )
    lines = [header]
    if findings:
        lines += ["", "DRIFT:"] + [f"- {f}" for f in findings]
    if errors:
        lines += ["", "CHECK ERRORS (result incomplete):"] + [f"- {e}" for e in errors]
    if sections.get("switched-off"):
        lines += ["", "SWITCHED OFF (stack or stack/service [profile]):"] + [f"  {i}" for i in sections["switched-off"]]
    if sections.get("on-demand"):
        lines += ["", "ON DEMAND (stack or stack/service [profile]):"] + [f"  {i}" for i in sections["on-demand"]]
    report = "\n".join(lines)

    print(report)
    if uuid:
        ping(uuid, f"/{code}", report)
    return code


if __name__ == "__main__":
    sys.exit(main())
