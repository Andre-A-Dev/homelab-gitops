#!/usr/bin/env python3
"""Turn a list of changed repository paths into a per-stack deploy plan.

Reads repo-relative paths (one per line) on stdin and prints one
tab-separated line per affected stack:

    <stack> <compose_changed 0|1> <recreate services> <build services> <note> <unmapped files>

"unmapped" lists changed files that no running service mounts or builds
from. They reach no container, so the handler reports them as a manual
follow-up instead of letting the deploy look complete.

Empty service lists are written as "-": bash `read` with IFS=$'\\t' treats
tab as whitespace and would silently collapse empty fields.

Why services are resolved per changed file instead of a plain `up -d` per
stack: a config file bind-mounted as a single file stays on its old inode
inside the running container after a git checkout, and `docker compose up -d`
does not recreate a service whose compose definition is unchanged. The old
handler only reacted to docker-compose.yml changes, so alerts.yml,
alertmanager.yml.tmpl or a Caddyfile edit never reached the container.
Only --force-recreate of exactly the services mounting the file picks it up.
"""

import json
import os
import subprocess
import sys
from collections import defaultdict

STACKS_DIR = "/home/<user>/stacks"
PREFIX = "mnemosyne/stacks/"

# Never touched automatically: the runner executing this very deploy, and the
# Gitea instance that runner reports to. Recreating either mid-job kills the
# job that is waiting for this handler's answer.
MANUAL_STACKS = {"gitea", "gitea-runner"}


def ignored(rest):
    """Files that change nothing at runtime."""
    return (
        rest.endswith(".md")
        or os.path.basename(rest) == ".env.example"
        # Directory mount, read by Grafana's provisioning loop - no inode issue.
        or "/grafana/dashboards/" in "/" + rest
    )


def inside(path, root):
    return path == root or path.startswith(root + os.sep)


def emit(stack, compose_changed, recreate, build, note, unmapped=()):
    print("\t".join([
        stack,
        "1" if compose_changed else "0",
        " ".join(sorted(recreate)) or "-",
        " ".join(sorted(build)) or "-",
        note,
        ",".join(sorted(unmapped)) or "-",
    ]))


def plan_stack(stack, files):
    stack_dir = os.path.join(STACKS_DIR, stack)

    if stack in MANUAL_STACKS:
        emit(stack, False, (), (), "manual")
        return True
    if not os.path.isfile(os.path.join(stack_dir, "docker-compose.yml")):
        # Stack removed from the repo, or a directory that is not a stack.
        # Either way a human has to decide - never guess here.
        emit(stack, False, (), (), "no-compose")
        return True

    try:
        result = subprocess.run(
            ["docker", "compose", "config", "--format", "json"],
            cwd=stack_dir, capture_output=True, text=True, check=True,
        )
        config = json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        stderr = getattr(exc, "stderr", "") or str(exc)
        print(f"{stack}: docker compose config failed: {stderr.strip()}", file=sys.stderr)
        emit(stack, False, (), (), "config-error")
        return False

    compose_changed = "docker-compose.yml" in files
    # realpath on both sides: ~/stacks is a symlink into the repo, and some
    # stacks (jobiris) mount the repo path directly. Comparing unresolved
    # strings would miss exactly those.
    changed = {
        os.path.realpath(os.path.join(stack_dir, f)): f
        for f in files if f != "docker-compose.yml"
    }
    mapped = set()

    recreate, build = set(), set()
    for name, service in (config.get("services") or {}).items():
        # Services behind a profile (e.g. jobiris-monitor, profile "cli") are
        # not running. Naming one in `up --force-recreate` would START it.
        startable = not service.get("profiles")

        for volume in service.get("volumes") or []:
            if volume.get("type") != "bind":
                continue
            source = os.path.realpath(volume["source"])
            hits = {f for f in changed if inside(f, source)}
            if startable and hits:
                recreate.add(name)
                mapped |= hits

        context = (service.get("build") or {}).get("context")
        if context:
            context = os.path.realpath(context)
            hits = {f for f in changed if inside(f, context)}
            if hits:
                # Building a profiled service is harmless - it only refreshes
                # the image that the running services may share.
                build.add(name)
                mapped |= hits
                if startable:
                    recreate.add(name)

    unmapped = [changed[f] for f in changed if f not in mapped]
    if compose_changed or recreate or build:
        emit(stack, compose_changed, recreate, build, "deploy", unmapped)
    else:
        emit(stack, False, (), (), "no-service-affected", unmapped)
    return True


def main():
    by_stack = defaultdict(list)
    for line in sys.stdin:
        path = line.strip()
        if not path.startswith(PREFIX):
            continue
        parts = path[len(PREFIX):].split("/", 1)
        if len(parts) < 2 or not parts[1]:
            continue  # file directly under stacks/, e.g. README.md
        stack, rest = parts
        if not ignored(rest):
            by_stack[stack].append(rest)

    ok = True
    for stack in sorted(by_stack):
        ok = plan_stack(stack, by_stack[stack]) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
