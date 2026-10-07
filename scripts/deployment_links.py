#!/usr/bin/env python3
"""Print deployment links *from the API payload*, never from memory.

The mistake this file exists to make impossible: a hostname typed into prose by
hand. It happened -- `cela-av4pcsc71` was written in a message while the real
deployment was `cela-av4vpcs71` -- and the typo'd slug does not exist, so the
published link led to `DEPLOYMENT_NOT_FOUND`. The lesson is not "check the link
more carefully"; it is that a link which passes through a human hand is a link
that can be corrupted by that hand. So links are produced by a program that has
no memory: it reads the payload, and every URL it prints is a value it found
there. The self-test asserts exactly that property -- every URL in the output is
in the input -- which is why this file is worth more than the apology.

    python scripts/deployment_links.py --repo owner/name --limit 8
    python scripts/deployment_links.py --markdown
    python scripts/deployment_links.py --self-test        # no network

Auth: uses `gh api` when `gh` is on PATH (so it works for private repositories
too), and falls back to an unauthenticated `urllib` request, which is enough for
a public repository. It never reads a token from a file, and it never prints one.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPO = "sayedelazameydesign-crypto/1pro"
NO_URL = "--"


def _gh_api(path: str) -> list | dict:
    """One GET through the CLI, which is where the credential already lives."""
    result = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"gh api {path} failed: {result.stderr.strip()[:400]}")
    return json.loads(result.stdout)


def _http_api(path: str) -> list | dict:
    import urllib.request

    request = urllib.request.Request(
        "https://api.github.com" + path,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "deployment-links"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed host
        return json.load(response)


def fetch(path: str, offline: bool = False) -> list | dict:
    if offline:
        raise RuntimeError("offline mode builds no payload")
    return _gh_api(path) if shutil.which("gh") else _http_api(path)


def newest_status(statuses: list) -> dict:
    """The latest status of one deployment, by creation time.

    The API returns them newest-first for `deployments/{id}/statuses`, but sorting
    by `created_at` means an ordering change cannot silently change the answer.
    """
    ordered = sorted(statuses or [], key=lambda item: item.get("created_at", ""))
    return ordered[-1] if ordered else {}


def rows(deployments: list, statuses: dict[int, list], limit: int = 8) -> list[dict]:
    """Shape the payload into rows. Every field here is copied, none composed."""
    out = []
    for deployment in (deployments or [])[:limit]:
        status = newest_status(statuses.get(deployment.get("id"), []))
        sha = deployment.get("sha") or ""
        out.append({
            "sha": sha,
            "short": sha[:7] or "--",
            "id": deployment.get("id"),
            "ref": deployment.get("ref") or "",
            "environment": deployment.get("environment") or "",
            "state": status.get("state") or "unknown",
            "url": status.get("environment_url") or NO_URL,
            "created_at": deployment.get("created_at") or "",
        })
    return out


def collect(repo: str, limit: int, offline: bool = False) -> list[dict]:
    deployments = fetch(f"/repos/{repo}/deployments?per_page={limit}", offline=offline)
    statuses = {
        deployment["id"]: fetch(f"/repos/{repo}/deployments/{deployment['id']}/statuses",
                                offline=offline)
        for deployment in deployments
    }
    return rows(deployments, statuses, limit=limit)


def render(rows_: list[dict], markdown: bool = False) -> str:
    lines = []
    if markdown:
        lines += ["| Commit | Deployment | State | Link |", "| --- | --- | --- | --- |"]
        for row in rows_:
            link = row["url"] if row["url"] != NO_URL else NO_URL
            lines.append(f"| `{row['short']}` | `{row['id']}` | {row['state']} | {link} |")
    else:
        for row in rows_:
            lines.append(f"{row['short']}  {row['id']}  {row['state']:<9} {row['url']}")
    return "\n".join(lines)


def extract_urls(payload) -> set[str]:
    """Every URL-shaped string anywhere in a payload, however it is nested."""
    found: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str) and node.startswith(("http://", "https://")):
            found.add(node)

    walk(payload)
    return found


def self_test() -> int:
    """The property that matters: this program cannot print a URL it was not given.

    A plausible-looking deployment is dangled in front of it -- a url on a status
    that is *older* than the newest one, and a deployment whose statuses carry no
    url at all -- and the output must contain one and only one URL, the newest
    recorded one, plus the placeholder for the one that has none. Nothing else,
    and nothing that a writer could have made up.
    """
    payload = [
        {"id": 2, "sha": "b" * 40, "ref": "main", "environment": "Production",
         "created_at": "2026-10-07T14:23:51Z"},
        {"id": 1, "sha": "a" * 40, "ref": "main", "environment": "Production",
         "created_at": "2026-10-07T12:00:00Z"},
    ]
    statuses = {
        2: [
            {"state": "success", "created_at": "2026-10-07T14:24:00Z",
             "environment_url": "https://newest.example.invalid"},
            {"state": "pending", "created_at": "2026-10-07T14:23:55Z",
             "environment_url": "https://stale.example.invalid"},
        ],
        1: [],
    }
    out = rows(payload, statuses, limit=8)
    text = render(out) + "\n" + render(out, markdown=True)

    checks = [
        (len(out) == 2, "one row per deployment"),
        (out[0]["url"] == "https://newest.example.invalid", "the row takes the newest status"),
        (out[1]["url"] == NO_URL, "a deployment with no status gets the placeholder, not a guess"),
        ("stale.example.invalid" not in text, "a stale status url is not published"),
    ]
    urls = extract_urls([payload, statuses])
    printed = {token.strip("|` ") for token in text.split()
               if token.startswith(("http://", "https://"))}
    checks.append((printed == {"https://newest.example.invalid"},
                   f"every url printed is one that was fetched: {printed}"))
    checks.append((printed <= urls, "and it is a subset of the payload's urls"))

    failed = [name for ok, name in checks if not ok]
    for ok, name in checks:
        print(("  ok   " if ok else "  FAIL ") + name)
    print("self-test " + ("OK" if not failed else "FAILED: " + ", ".join(failed)))
    return 0 if not failed else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--markdown", action="store_true", help="emit a table for a PR body")
    parser.add_argument("--self-test", action="store_true", help="check the no-invented-link rule")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    try:
        print(render(collect(args.repo, args.limit), markdown=args.markdown))
    except (RuntimeError, OSError) as error:
        print(f"could not read the deployments API: {error}", file=sys.stderr)
        print("no link is better than an unverified one -- nothing was printed.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
