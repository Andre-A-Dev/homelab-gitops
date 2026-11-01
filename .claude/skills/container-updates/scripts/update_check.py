#!/usr/bin/env python3
"""
update_check.py — data for the container-updates skill.

Read-only. Combines three sources:
  - Prometheus on Mnemosyne (read-only SSH alias): container_image_update_available
    with the running digest (label local_digest), written daily by
    mnemosyne/scripts/container-update-metrics.sh
  - the compose files in this repo: update class, Diun labels, build services
  - the image registries, anonymously: digests via HEAD (does not count
    against Docker Hub's pull limit), tag lists, and the OCI labels of at most
    two images per update (those GETs do count)

    update_check.py                     report: digest updates, new tags
                                        beyond the pin, class C, errors
    update_check.py --image REF [--digest sha256:...]
                                        resolve one image: version behind the
                                        tag now, and behind DIGEST if given

A digest becomes a version by its OCI label org.opencontainers.image.version,
or else by matching it against the more specific tags of the same repository
(gitea/gitea:28 has the digest of 28.x.y). Neither works for every image;
"unknown" is then the honest answer.

Exit code: 1 if Prometheus or the repo could not be read, else 0.
"""

import argparse
import json
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[4]
STACKS = REPO / "mnemosyne" / "stacks"
PROM_HOST = "mnemosyne-ro"
ARCH = "arm64"                      # Mnemosyne is a Pi 5
FLOATING = {"latest", "stable", "release", "lts"}
MAX_PROBES = 40                     # HEAD requests per digest-to-tag lookup
UA = "homelab-update-check/1"
ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])
INDEX_TYPES = ("application/vnd.oci.image.index.v1+json",
               "application/vnd.docker.distribution.manifest.list.v2+json")


class RegistryError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # Redirects are followed by hand: a token for one host must not travel to
    # another (lscr.io -> ghcr.io, Docker Hub blobs -> CDN with signed URLs).
    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(_NoRedirect)


class Registry:
    def __init__(self):
        self.tokens = {}

    @staticmethod
    def _key(url):
        u = urllib.parse.urlparse(url)
        m = re.match(r"^/v2/(.+?)/(manifests|tags|blobs)/", u.path)
        return (u.netloc, m.group(1) if m else "")

    def _token(self, challenge):
        if not challenge.lower().startswith("bearer"):
            return None
        params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        realm = params.pop("realm", None)
        if not realm:
            return None
        url = realm + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
        return data.get("token") or data.get("access_token")

    def request(self, url, method="GET", accept=ACCEPT, _hops=0):
        key = self._key(url)
        for attempt in (1, 2):
            headers = {"Accept": accept, "User-Agent": UA}
            if key in self.tokens:
                headers["Authorization"] = f"Bearer {self.tokens[key]}"
            req = urllib.request.Request(url, method=method, headers=headers)
            try:
                with OPENER.open(req, timeout=20) as r:
                    body = r.read() if method != "HEAD" else b""
                    return r.headers, body
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 303, 307, 308) and _hops < 5:
                    target = urllib.parse.urljoin(url, e.headers["Location"])
                    return self.request(target, method, accept, _hops + 1)
                if e.code == 401 and attempt == 1:
                    tok = self._token(e.headers.get("WWW-Authenticate", ""))
                    if tok:
                        self.tokens[key] = tok
                        continue
                raise RegistryError(f"{method} {url}: HTTP {e.code}") from None
            except urllib.error.URLError as e:
                raise RegistryError(f"{method} {url}: {e.reason}") from None
        raise RegistryError(f"{method} {url}: unauthorized")

    def digest(self, api, repo, ref):
        headers, _ = self.request(f"https://{api}/v2/{repo}/manifests/{ref}", "HEAD")
        d = headers.get("Docker-Content-Digest")
        if not d:
            raise RegistryError(f"{api}/{repo}:{ref}: no Docker-Content-Digest header")
        return d

    def tags(self, api, repo):
        url = f"https://{api}/v2/{repo}/tags/list?n=1000"
        out = []
        for _ in range(50):
            headers, body = self.request(url, accept="application/json")
            out += json.loads(body).get("tags") or []
            m = re.search(r'<([^>]+)>;\s*rel="next"', headers.get("Link", ""))
            if not m:
                break
            url = urllib.parse.urljoin(url, m.group(1))
        return out

    def config(self, api, repo, ref):
        """Image config (created, labels) for linux/ARCH. Costs two pulls on Docker Hub."""
        _, body = self.request(f"https://{api}/v2/{repo}/manifests/{ref}")
        man = json.loads(body)
        if man.get("mediaType") in INDEX_TYPES or "manifests" in man:
            plat = [m for m in man.get("manifests", [])
                    if m.get("platform", {}).get("os") == "linux"
                    and m.get("platform", {}).get("architecture") == ARCH]
            if not plat:
                return {}
            _, body = self.request(f"https://{api}/v2/{repo}/manifests/{plat[0]['digest']}")
            man = json.loads(body)
        cfg_digest = man.get("config", {}).get("digest")
        if not cfg_digest:
            return {}
        _, body = self.request(f"https://{api}/v2/{repo}/blobs/{cfg_digest}",
                               accept="application/json")
        cfg = json.loads(body)
        return {"created": (cfg.get("created") or "")[:10],
                "labels": (cfg.get("config") or {}).get("Labels") or {}}


def parse_ref(ref):
    """'gitea/gitea:28' -> ('registry-1.docker.io', 'gitea/gitea', '28')."""
    name = ref.split("@", 1)[0]
    tag = "latest"
    if name.rfind(":") > name.rfind("/"):
        name, tag = name.rsplit(":", 1)
    first = name.split("/", 1)[0]
    if "/" in name and ("." in first or ":" in first or first == "localhost"):
        registry, repo = first, name.split("/", 1)[1]
    else:
        registry, repo = "docker.io", name
    if registry == "docker.io":
        registry = "registry-1.docker.io"
        if "/" not in repo:
            repo = "library/" + repo
    return registry, repo, tag


def vkey(tag):
    return tuple(int(x) for x in re.findall(r"\d+", tag))


def candidates(pin, tags):
    """More specific tags that can share the pin's digest, newest first."""
    if pin in FLOATING:
        pat = r"^v?\d+\.\d+\.\d+$"
    else:
        m = re.match(r"^(v?)(\d+(?:\.\d+)*)(.*)$", pin)
        if not m:
            return []
        pre, ver, suf = m.groups()
        pat = rf"^{pre}{re.escape(ver)}(?:\.\d+)*{re.escape(suf)}(?:-ls\d+)?$"
    return sorted((t for t in tags if t != pin and re.match(pat, t)),
                  key=lambda t: (vkey(t), len(t)), reverse=True)


def version_of(reg, api, repo, digest, cands, want_labels=True):
    """(version, created, source) for a digest; version may be 'unknown'."""
    created = source = ""
    if want_labels:
        try:
            cfg = reg.config(api, repo, digest)
            labels = cfg.get("labels", {})
            created = cfg.get("created", "")
            source = labels.get("org.opencontainers.image.source", "")
            v = labels.get("org.opencontainers.image.version")
            if v:
                return v, created, source
        except RegistryError:
            pass  # digest no longer in the registry: fall through to tag matching
    for tag in cands[:MAX_PROBES]:
        try:
            if reg.digest(api, repo, tag) == digest:
                return tag, created, source
        except RegistryError:
            continue
    return "unknown", created, source


def prom(query):
    cmd = ("curl -s http://localhost:9090/api/v1/query --data-urlencode "
           + shlex.quote("query=" + query))
    r = subprocess.run(["ssh", PROM_HOST, cmd], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"ssh {PROM_HOST}: rc={r.returncode} {r.stderr.strip()}")
    data = json.loads(r.stdout)
    if data.get("status") != "success":
        raise RuntimeError(f"Prometheus: {data}")
    return data["data"]["result"]


def compose_index():
    """container name -> service info, from every Mnemosyne compose file."""
    index = {}
    for f in sorted(STACKS.glob("*/docker-compose.yml")):
        doc = yaml.safe_load(f.read_text()) or {}
        stack = f.parent.name
        project = doc.get("name") or stack
        for svc, spec in (doc.get("services") or {}).items():
            labels = spec.get("labels") or {}
            if isinstance(labels, list):
                labels = dict(i.split("=", 1) if "=" in i else (i, "") for i in labels)
            info = {"stack": stack, "service": svc, "labels": labels,
                    "image": spec.get("image", "")}
            for name in (spec.get("container_name"), f"{project}-{svc}-1"):
                if name:
                    index[name] = info
    return index


def short(d):
    return d.split(":", 1)[-1][:12] if d else "-"


def report(reg):
    index = compose_index()
    rows = prom("container_image_update_available")
    # Running containers only. Status 3 with no "/" in the image is a local
    # build with an explicit tag (carousel:latest) that the metrics script
    # misfiles as a registry error.
    builds, errors = [], []
    for r in prom("container_image_check_status >= 2"):
        m = r["metric"]
        local = r["value"][1] == "2" or "/" not in m.get("image", "")
        (builds if local else errors).append(m)
    ts = prom("container_update_check_timestamp_seconds")
    if ts:
        age_h = (time.time() - float(ts[0]["value"][1])) / 3600
        print(f"Metric check ran {age_h:.1f} h ago ({len(rows)} containers checked).")
    print()

    print("== Digest updates: same tag, new image ==")
    updates = [r["metric"] for r in rows if r["value"][1] == "1"]
    if not updates:
        print("none")
    for m in sorted(updates, key=lambda m: m["container"]):
        info = index.get(m["container"], {})
        cls = info.get("labels", {}).get("homelab.update-class", "?")
        where = f"{info['stack']}/{info['service']}" if info else "not in repo"
        print(f"\n{m['container']}  {m['image']}  class {cls}  ({where})")
        api, repo, tag = parse_ref(m["image"])
        try:
            cands = candidates(tag, reg.tags(api, repo))
            now = reg.digest(api, repo, tag)
        except RegistryError as e:
            print(f"  registry error: {e}")
            continue
        local = m.get("local_digest", "")
        if local and local == now:
            print("  already up to date: the running digest equals the tag now "
                  "(updated after the metric check)")
            continue
        new_v, new_created, source = version_of(reg, api, repo, now, cands)
        if local:
            old_v, old_created, _ = version_of(reg, api, repo, local, cands)
        else:
            old_v, old_created = "unknown (metric has no local_digest label yet)", ""
        print(f"  running:   {old_v}  {short(local)}  {old_created}")
        print(f"  available: {new_v}  {short(now)}  built {new_created or '?'}")
        if source:
            print(f"  source:    {source}")

    print("\n== New tags beyond the pin (diun.watch_repo) ==")
    found = False
    seen = set()
    for info in index.values():
        labels = info["labels"]
        key = (info["stack"], info["service"])
        if key in seen or str(labels.get("diun.watch_repo")).lower() != "true":
            continue
        seen.add(key)
        api, repo, pin = parse_ref(info["image"])
        rx = re.compile(str(labels.get("diun.include_tags", "")).replace("$$", "$"))
        try:
            tags = reg.tags(api, repo)
        except RegistryError as e:
            print(f"{info['stack']}/{info['service']}: registry error: {e}")
            continue
        newer = sorted((t for t in tags if rx.search(t) and t != pin and vkey(t) > vkey(pin)),
                       key=vkey)
        cls = labels.get("homelab.update-class", "?")
        if newer:
            found = True
            print(f"{info['stack']}/{info['service']}  {info['image']}  class {cls}  "
                  f"-> {', '.join(newer[-5:])}")
    if not found:
        print("none")

    print("\n== Class C (running local builds): base images not checked, "
          "rebuild with --pull ==")
    stacks = sorted({index.get(m["container"], {}).get("stack", m["container"]) for m in builds})
    print(", ".join(stacks) if stacks else "none")

    print("\n== Registry errors (status 3) ==")
    if not errors:
        print("none")
    for m in errors:
        print(f"{m.get('container')}  {m.get('image')}")


def single(reg, ref, digest):
    api, repo, tag = parse_ref(ref)
    cands = candidates(tag, reg.tags(api, repo))
    now = reg.digest(api, repo, tag)
    v, created, source = version_of(reg, api, repo, now, cands)
    print(f"{ref}: tag now {short(now)} = {v}, built {created or '?'}  {source}")
    if digest:
        v, created, _ = version_of(reg, api, repo, digest, cands)
        print(f"{ref}: digest {short(digest)} = {v}, built {created or '?'}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--image", help="resolve one image reference instead of the report")
    ap.add_argument("--digest", help="with --image: also resolve this digest")
    args = ap.parse_args()
    reg = Registry()
    try:
        if args.image:
            single(reg, args.image, args.digest)
        else:
            report(reg)
    except (RuntimeError, OSError, yaml.YAMLError, RegistryError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
