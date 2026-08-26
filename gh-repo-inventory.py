			'By using the following materials or sample code you agree to be bound by the license terms below 
			'and the Microsoft Partner Program Agreement the terms of which are incorporated herein by this reference. 
			'These license terms are an agreement between Microsoft Corporation (or, if applicable based on where you 
			'are located, one of its affiliates) and you. Any materials (other than sample code) we provide to you 
			'are for your internal use only. Any sample code is provided for the purpose of illustration only and is 
			'not intended to be used in a production environment. We grant you a nonexclusive, royalty-free right to 
			'use and modify the sample code and to reproduce and distribute the object code form of the sample code, 
			'provided that you agree: (i) to not use Microsoft’s name, logo, or trademarks to market your software product 
			'in which the sample code is embedded; (ii) to include a valid copyright notice on your software product in 
			'which the sample code is embedded; (iii) to provide on behalf of and for the benefit of your subcontractors 
			'a disclaimer of warranties, exclusion of liability for indirect and consequential damages and a reasonable 
			'limitation of liability; and (iv) to indemnify, hold harmless, and defend Microsoft, its affiliates and 
			'suppliers from and against any third party claims or lawsuits, including attorneys’ fees, that arise or result 
			'from the use or distribution of the sample code."


#!/usr/bin/env python3
"""
gh-repo-inventory — organisation repository & size inventory for GitHub migrations.

A dependency-free alternative to `gh repo-stats` for environments where tools
cannot be downloaded or installed.


WHAT IT PRODUCES
----------------
A CSV (or JSON / console table) with one row per repository, including the
size and migration-risk columns needed to plan a GEI migration:

  org, repo, visibility, is_archived, is_fork, is_empty, size_kb, size_mb,
  size_band, default_branch, created_at, last_push, branches, tags, releases,
  open_issues, prs, collaborators, teams, webhooks, environments, workflows,
  secrets, variables, has_wiki, has_lfs, migration_risk, url

USAGE
-----
  export GH_TOKEN=ghp_xxxx

  # single org on GitHub Enterprise Server
  python3 gh-repo-inventory.py --org my-org --hostname github.internal.com

  # every org on the instance
  python3 gh-repo-inventory.py --all-orgs --hostname github.internal.com

  # several orgs from a file (one per line)
  python3 gh-repo-inventory.py --input orgs.txt --hostname github.internal.com

  # GHE.com (data residency) target-side check
  python3 gh-repo-inventory.py --org my-org --hostname acme.ghe.com

  # fast pass: sizes only, skips the per-repo detail calls
  python3 gh-repo-inventory.py --org my-org --hostname github.internal.com --fast

  # console summary while it runs
  python3 gh-repo-inventory.py --org my-org --hostname github.internal.com --format table

EXIT CODES
  0 success   1 usage/auth error   2 completed with per-repo errors
"""

import argparse
import csv
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

__version__ = "1.0"

# Archive size limits by GHES major.minor. Used only to flag migration risk.
# Confirm the exact limit for your GHES version before relying on this.
SIZE_LIMIT_GB_DEFAULT = 10.0

COLUMNS = [
    "org", "repo", "visibility", "is_archived", "is_fork", "is_empty",
    "size_kb", "size_mb", "size_band", "default_branch", "created_at", "last_push",
    "branches", "tags", "releases", "open_issues", "prs", "collaborators",
    "teams", "webhooks", "environments", "workflows", "secrets", "variables",
    "has_wiki", "has_lfs", "migration_risk", "url",
]


class Client:
    """Minimal GitHub REST client: auth, pagination, rate limiting, retries."""

    def __init__(self, hostname, token, insecure=False, verbose=False):
        self.verbose = verbose
        self.token = token
        host = hostname.replace("https://", "").replace("http://", "").strip("/")
        self.host = host
        # Endpoint shape differs by product:
        #   GitHub.com          -> api.github.com
        #   GHE.com (residency) -> api.SUBDOMAIN.ghe.com
        #   GHES                -> HOST/api/v3
        if host == "github.com":
            self.base = "https://api.github.com"
        elif host.endswith(".ghe.com"):
            self.base = "https://api.%s" % host
        else:
            self.base = "https://%s/api/v3" % host
        self.ctx = None
        if insecure:
            self.ctx = ssl.create_default_context()
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE
        self.calls = 0

    def _request(self, url, retries=3):
        req = urllib.request.Request(url)
        req.add_header("Authorization", "token %s" % self.token)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "gh-repo-inventory/%s" % __version__)

        for attempt in range(retries):
            try:
                self.calls += 1
                with urllib.request.urlopen(req, timeout=60, context=self.ctx) as r:
                    body = r.read().decode("utf-8")
                    remaining = r.headers.get("X-RateLimit-Remaining")
                    if remaining is not None and remaining.isdigit() and int(remaining) < 50:
                        reset = int(r.headers.get("X-RateLimit-Reset", 0))
                        wait = max(0, reset - int(time.time())) + 5
                        if wait > 0 and wait < 3700:
                            sys.stderr.write(
                                "  rate limit low (%s left) — pausing %ss\n" % (remaining, wait))
                            time.sleep(wait)
                    return json.loads(body) if body else None, r.headers
            except urllib.error.HTTPError as e:
                if e.code in (403, 429):
                    reset = e.headers.get("X-RateLimit-Reset")
                    wait = 60
                    if reset and reset.isdigit():
                        wait = max(5, int(reset) - int(time.time()) + 5)
                    if attempt < retries - 1 and wait < 3700:
                        sys.stderr.write("  throttled — waiting %ss\n" % wait)
                        time.sleep(wait)
                        continue
                # 404/409 are normal (no access, empty repo) — surface quietly
                raise
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < retries - 1:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise
        return None, None

    def get(self, path, default=None):
        """Single GET. Returns `default` on any HTTP error."""
        url = path if path.startswith("http") else self.base + path
        try:
            data, _ = self._request(url)
            return data
        except Exception:
            return default

    def count(self, path):
        """Cheap count via per_page=1 and the Link rel=last header."""
        url = self.base + path
        sep = "&" if "?" in path else "?"
        url = url + sep + "per_page=1"
        try:
            data, headers = self._request(url)
        except Exception:
            return -1
        if data is None:
            return -1
        link = headers.get("Link", "") if headers else ""
        if 'rel="last"' in link:
            for part in link.split(","):
                if 'rel="last"' in part:
                    seg = part[part.find("<") + 1:part.find(">")]
                    # Match page= anywhere in the segment. GHES versions differ in
                    # whether the Link header carries a fully-qualified URL, so do
                    # not rely on urlparse finding a query string.
                    m = re.search(r"[?&]page=(\d+)", seg)
                    if m:
                        return int(m.group(1))
                    return -1
        if isinstance(data, list):
            return len(data)
        return -1

    def paginate(self, path):
        """Yield every item across all pages."""
        page = 1
        while True:
            sep = "&" if "?" in path else "?"
            url = "%s%s%sper_page=100&page=%d" % (self.base, path, sep, page)
            try:
                data, _ = self._request(url)
            except Exception:
                return
            if not data or not isinstance(data, list):
                return
            for item in data:
                yield item
            if len(data) < 100:
                return
            page += 1

    def paginate_since(self, path):
        """Pagination for endpoints that ignore `page` and use `since=<last id>`."""
        since = 0
        while True:
            sep = "&" if "?" in path else "?"
            url = "%s%s%sper_page=100&since=%d" % (self.base, path, sep, since)
            try:
                data, _ = self._request(url)
            except Exception:
                return
            if not data or not isinstance(data, list):
                return
            for item in data:
                yield item
            last = data[-1].get("id")
            if not isinstance(last, int) or last <= since:
                return
            since = last


def band(size_kb):
    mb = size_kb / 1024.0
    if mb < 500:
        return "small"
    if mb < 5120:
        return "medium"
    if mb < 20480:
        return "large"
    return "xl"


def scan_repo(c, org, repo, fast, limit_gb):
    name = repo.get("name")
    size_kb = repo.get("size") or 0
    size_mb = round(size_kb / 1024.0, 2)
    row = {k: "" for k in COLUMNS}
    row.update({
        "org": org,
        "repo": name,
        "visibility": repo.get("visibility") or ("private" if repo.get("private") else "public"),
        "is_archived": repo.get("archived", False),
        "is_fork": repo.get("fork", False),
        "is_empty": size_kb == 0,
        "size_kb": size_kb,
        "size_mb": size_mb,
        "size_band": band(size_kb),
        "default_branch": repo.get("default_branch") or "",
        "created_at": repo.get("created_at") or "",
        "last_push": repo.get("pushed_at") or "",
        "open_issues": repo.get("open_issues_count", 0),
        "has_wiki": repo.get("has_wiki", False),
        "url": repo.get("html_url") or "",
    })

    risks = []
    if size_mb / 1024.0 > limit_gb:
        risks.append("EXCEEDS_%sGB_LIMIT" % limit_gb)
    if size_kb == 0:
        risks.append("EMPTY")
    if repo.get("fork"):
        risks.append("FORK_RELATIONSHIP_NOT_MIGRATED")

    if fast:
        row["migration_risk"] = ";".join(risks) if risks else "OK"
        return row, 0

    errors = 0
    base = "/repos/%s/%s" % (org, name)

    row["branches"] = c.count(base + "/branches")
    row["tags"] = c.count(base + "/tags")
    row["releases"] = c.count(base + "/releases")
    row["prs"] = c.count(base + "/pulls?state=all")
    row["collaborators"] = c.count(base + "/collaborators?affiliation=direct")
    row["teams"] = c.count(base + "/teams")
    row["webhooks"] = c.count(base + "/hooks")

    envs = c.get(base + "/environments", {}) or {}
    row["environments"] = envs.get("total_count", 0) if isinstance(envs, dict) else 0

    wf = c.get(base + "/actions/workflows", {}) or {}
    row["workflows"] = wf.get("total_count", 0) if isinstance(wf, dict) else 0

    sec = c.get(base + "/actions/secrets", {}) or {}
    row["secrets"] = sec.get("total_count", 0) if isinstance(sec, dict) else 0

    var = c.get(base + "/actions/variables", {}) or {}
    row["variables"] = var.get("total_count", 0) if isinstance(var, dict) else 0

    lfs = c.get(base + "/contents/.gitattributes")
    row["has_lfs"] = bool(lfs)

    if row["webhooks"] and row["webhooks"] > 0:
        risks.append("WEBHOOKS_ARRIVE_DISABLED")
    if row["secrets"] and row["secrets"] > 0:
        risks.append("SECRETS_NOT_MIGRATED")
    if row["teams"] and row["teams"] > 0:
        risks.append("TEAM_ACCESS_NOT_MIGRATED")
    if row["has_lfs"]:
        risks.append("LFS_NOT_MIGRATED")
    if row["environments"] and row["environments"] > 0:
        risks.append("ENVIRONMENTS_NOT_MIGRATED")

    row["migration_risk"] = ";".join(risks) if risks else "OK"
    return row, errors


def main():
    p = argparse.ArgumentParser(
        description="Organisation repository & size inventory for GitHub migrations.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("-o", "--org", help="organisation to scan")
    g.add_argument("-i", "--input", help="file containing organisations, one per line")
    g.add_argument("-a", "--all-orgs", action="store_true",
                   help="scan every organisation on the instance (GHES site admin)")
    p.add_argument("-H", "--hostname", default="github.com",
                   help="github.com | GHES host | SUBDOMAIN.ghe.com   (default: github.com)")
    p.add_argument("-t", "--token", default=os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"),
                   help="PAT; defaults to $GH_TOKEN / $GITHUB_TOKEN")
    p.add_argument("-O", "--output", default="repo-inventory.csv", help="output file")
    p.add_argument("-f", "--format", choices=["csv", "json", "table"], default="csv")
    p.add_argument("--fast", action="store_true",
                   help="sizes only — 1 API call per org page instead of ~12 per repo")
    p.add_argument("--size-limit-gb", type=float, default=SIZE_LIMIT_GB_DEFAULT,
                   help="archive size limit to flag against (default: %(default)s)")
    p.add_argument("--repo-list", help="file of repo names to restrict the scan to")
    p.add_argument("--insecure", action="store_true", help="skip TLS verification (internal CA)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version="gh-repo-inventory %s" % __version__)
    a = p.parse_args()

    if not a.token:
        sys.stderr.write("ERROR: no token. Set GH_TOKEN or pass --token.\n")
        return 1

    c = Client(a.hostname, a.token, insecure=a.insecure, verbose=a.verbose)
    sys.stderr.write("gh-repo-inventory %s\n" % __version__)
    sys.stderr.write("  endpoint : %s\n" % c.base)
    sys.stderr.write("  mode     : %s\n" % ("fast (sizes only)" if a.fast else "full"))

    who = c.get("/user")
    if not who:
        sys.stderr.write("ERROR: authentication failed against %s.\n" % c.base)
        sys.stderr.write("       For GHE.com the PAT must be created on that host, not github.com.\n")
        return 1
    sys.stderr.write("  auth as  : %s\n\n" % who.get("login", "?"))

    orgs = []
    if a.org:
        orgs = [a.org]
    elif a.input:
        with open(a.input) as fh:
            orgs = [l.strip() for l in fh if l.strip() and not l.startswith("#")]
    else:
        if c.host == "github.com":
            # /organizations on github.com lists every org on the public instance.
            sys.stderr.write(
                "NOTE: --all-orgs is a GHES/GHE.com site-admin feature. On github.com it\n"
                "      would enumerate every public organisation, so scanning only the\n"
                "      organisations this token belongs to. Use --org/--input to target.\n")
            orgs = [o.get("login") for o in c.paginate("/user/orgs") if o.get("login")]
        else:
            sys.stderr.write("Enumerating organisations...\n")
            orgs = [o.get("login") for o in c.paginate_since("/organizations") if o.get("login")]
        sys.stderr.write("  found %d\n\n" % len(orgs))

    only = None
    if a.repo_list:
        with open(a.repo_list) as fh:
            only = set(l.strip() for l in fh if l.strip())

    rows, errors = [], 0
    for org in orgs:
        sys.stderr.write("Scanning %s ...\n" % org)
        n = 0
        for repo in c.paginate("/orgs/%s/repos?type=all" % org):
            if only and repo.get("name") not in only:
                continue
            try:
                row, e = scan_repo(c, org, repo, a.fast, a.size_limit_gb)
                rows.append(row)
                errors += e
                n += 1
                if a.format == "table":
                    print("%-28s %10.2f MB  %-7s %s" % (
                        row["repo"][:28], row["size_mb"], row["size_band"], row["migration_risk"]))
                elif n % 25 == 0:
                    sys.stderr.write("  %d repositories...\n" % n)
            except Exception as ex:
                errors += 1
                sys.stderr.write("  ERROR %s/%s: %s\n" % (org, repo.get("name"), ex))
        sys.stderr.write("  %s: %d repositories\n" % (org, n))

    if not rows:
        sys.stderr.write("\nNo repositories found. Check the token scopes and org names.\n")
        return 1

    if a.format == "json":
        with open(a.output, "w") as fh:
            json.dump(rows, fh, indent=2)
    else:
        with open(a.output, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=COLUMNS)
            w.writeheader()
            w.writerows(rows)

    total_mb = sum(r["size_mb"] for r in rows)
    bands = {}
    for r in rows:
        bands[r["size_band"]] = bands.get(r["size_band"], 0) + 1
    flagged = [r for r in rows if r["migration_risk"] not in ("OK", "")]
    oversize = [r for r in rows if "EXCEEDS" in str(r["migration_risk"])]

    sys.stderr.write("\n" + "=" * 62 + "\n")
    sys.stderr.write("  Repositories : %d across %d organisation(s)\n" % (len(rows), len(orgs)))
    sys.stderr.write("  Total size   : %.2f GB\n" % (total_mb / 1024.0))
    sys.stderr.write("  API calls    : %d\n" % c.calls)
    sys.stderr.write("  Size bands   : %s\n" % ", ".join(
        "%s=%d" % (k, bands[k]) for k in ("small", "medium", "large", "xl") if k in bands))
    sys.stderr.write("  Flagged      : %d repositories with migration considerations\n" % len(flagged))
    if oversize:
        sys.stderr.write("  OVERSIZE     : %d exceed the %.0f GB limit — plan these separately:\n"
                         % (len(oversize), a.size_limit_gb))
        for r in oversize[:10]:
            sys.stderr.write("                 %s/%s  %.2f GB\n" % (r["org"], r["repo"], r["size_mb"] / 1024.0))
    sys.stderr.write("  Output       : %s\n" % a.output)
    sys.stderr.write("=" * 62 + "\n")

    return 2 if errors else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.stderr.write("\nInterrupted.\n")
        sys.exit(1)
