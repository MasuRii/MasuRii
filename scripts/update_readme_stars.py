#!/usr/bin/env python3
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


README_PATH = "README.md"
OWNER = "MasuRii"
PROJECT_STAR_THRESHOLD = 2
PROJECTS_HEADING = "## Projects"
OSS_HEADING = "## Open Source Contributions"
SECTION_DIVIDER = "---"
EXCLUDED_OSS_REPOS: set[str] = set()


def github_headers(token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "readme-metadata-updater",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def rest_get_json(url: str, token: str | None) -> Any | None:
    request = urllib.request.Request(url, headers=github_headers(token))
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        print(f"[warn] Request failed ({error.code}): {url}")
        return None
    except Exception as error:  # pragma: no cover
        print(f"[warn] Request failed: {url} ({error})")
        return None


def graphql_query(
    query: str, variables: dict[str, Any], token: str | None
) -> Any | None:
    if not token:
        print("[warn] Skipping GraphQL request: missing GITHUB_TOKEN/GH_TOKEN")
        return None

    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(
        "https://api.github.com/graphql",
        data=payload,
        headers=github_headers(token),
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.load(response)
        if "errors" in data:
            print(f"[warn] GraphQL errors: {data['errors']}")
            return None
        return data.get("data")
    except urllib.error.HTTPError as error:
        print(f"[warn] GraphQL request failed (HTTP {error.code})")
        return None
    except Exception as error:  # pragma: no cover
        print(f"[warn] GraphQL request failed ({error})")
        return None


def locate_section(lines: list[str], heading: str) -> tuple[int, int] | None:
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == heading)
    except StopIteration:
        return None

    for idx in range(start + 1, len(lines)):
        if lines[idx].strip() == SECTION_DIVIDER:
            return start, idx

    return start, len(lines)


def fetch_repo_stars(owner: str, repo: str, token: str | None) -> int | None:
    url = f"https://api.github.com/repos/{owner}/{repo}"
    payload = rest_get_json(url, token)
    if payload is None:
        return None
    return int(payload.get("stargazers_count", 0))


def update_project_stars(lines: list[str], token: str | None) -> bool:
    section = locate_section(lines, PROJECTS_HEADING)
    if section is None:
        print("[warn] Projects section not found")
        return False

    start, end = section
    repo_pattern = re.compile(r"https://github\.com/MasuRii/([A-Za-z0-9_.-]+)")
    star_cache: dict[str, int | None] = {}
    changed = False

    for idx in range(start + 1, end):
        line = lines[idx]
        if not line.lstrip().startswith("-"):
            continue

        match = repo_pattern.search(line)
        if not match:
            continue

        repo = match.group(1)
        if repo not in star_cache:
            star_cache[repo] = fetch_repo_stars(OWNER, repo, token)
            time.sleep(0.1)

        stars = star_cache[repo]
        if stars is None:
            continue

        base_line = re.sub(r"\s+`⭐\s*\d+`\s*$", "", line)
        if stars >= PROJECT_STAR_THRESHOLD:
            updated = f"{base_line} `⭐ {stars}`"
        else:
            updated = base_line

        if updated != line:
            lines[idx] = updated
            changed = True

    if changed:
        print("[ok] Project star markers updated")
    else:
        print("[ok] No project star marker changes needed")

    return changed


def fetch_contributed_repositories(token: str | None) -> list[str]:
    query = """
    query($login: String!, $cursor: String) {
      user(login: $login) {
        repositoriesContributedTo(
          first: 100
          after: $cursor
          includeUserRepositories: false
          contributionTypes: [PULL_REQUEST, ISSUE]
        ) {
          pageInfo {
            hasNextPage
            endCursor
          }
          nodes {
            nameWithOwner
          }
        }
      }
    }
    """

    repos: list[str] = []
    cursor: str | None = None

    while True:
        data = graphql_query(query, {"login": OWNER, "cursor": cursor}, token)
        if data is None:
            return []

        user = data.get("user")
        if not user:
            return repos

        block = user["repositoriesContributedTo"]
        nodes = block.get("nodes", [])
        repos.extend(
            node["nameWithOwner"] for node in nodes if node.get("nameWithOwner")
        )

        page_info = block.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")

    return repos


def fetch_pr_stats(
    repo_name_with_owner: str, token: str | None
) -> tuple[int, int] | None:
    query_base = f"repo:{repo_name_with_owner} is:pr author:{OWNER}"
    total_url = (
        "https://api.github.com/search/issues?q="
        + urllib.parse.quote_plus(query_base)
        + "&per_page=1"
    )
    merged_url = (
        "https://api.github.com/search/issues?q="
        + urllib.parse.quote_plus(query_base + " is:merged")
        + "&per_page=1"
    )

    total_payload = rest_get_json(total_url, token)
    if total_payload is None:
        return None
    time.sleep(0.15)

    merged_payload = rest_get_json(merged_url, token)
    if merged_payload is None:
        return None

    total = int(total_payload.get("total_count", 0))
    merged = int(merged_payload.get("total_count", 0))
    return total, merged


def format_pr_label(value: int) -> str:
    return f"{value} PR" if value == 1 else f"{value} PRs"


def format_merged_label(value: int) -> str:
    return f"{value} merged"


def build_oss_lines(token: str | None) -> list[str]:
    repos = fetch_contributed_repositories(token)
    if not repos:
        return []

    stats: list[tuple[str, int, int]] = []
    for repo in sorted(set(repos)):
        if repo in EXCLUDED_OSS_REPOS:
            continue

        pr_stats = fetch_pr_stats(repo, token)
        time.sleep(0.15)
        if pr_stats is None:
            continue

        pr_count, merged_count = pr_stats
        if pr_count <= 0:
            continue

        stats.append((repo, pr_count, merged_count))

    stats.sort(key=lambda item: (-item[2], -item[1], item[0].lower()))

    lines: list[str] = []
    for repo, pr_count, merged_count in stats:
        pr_label = format_pr_label(pr_count)
        merged_label = format_merged_label(merged_count)
        repo_url = f"https://github.com/{repo}"
        prs_url = f"https://github.com/{repo}/pulls?q=author%3A{OWNER}"
        lines.append(
            f"- 🔹 **[{repo}]({repo_url})** - PR contributions (`{pr_label}`, `{merged_label}`). [Pull Requests]({prs_url})"
        )

    return lines


def update_oss_section(lines: list[str], token: str | None) -> bool:
    section = locate_section(lines, OSS_HEADING)
    if section is None:
        print("[warn] OSS section not found")
        return False

    oss_lines = build_oss_lines(token)
    if not oss_lines:
        print("[warn] OSS section not updated (no PR data available)")
        return False

    start, end = section
    replacement = ["", *oss_lines, ""]
    new_lines = lines[: start + 1] + replacement + lines[end:]

    if new_lines == lines:
        print("[ok] No OSS contribution changes needed")
        return False

    lines[:] = new_lines
    print("[ok] OSS contribution entries refreshed from PR data")
    return True


def main() -> int:
    if not os.path.exists(README_PATH):
        print(f"[error] Missing {README_PATH}")
        return 1

    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")

    with open(README_PATH, "r", encoding="utf-8") as file:
        lines = file.read().splitlines()

    changed = False
    changed = update_project_stars(lines, token) or changed
    changed = update_oss_section(lines, token) or changed

    if changed:
        with open(README_PATH, "w", encoding="utf-8", newline="\n") as file:
            file.write("\n".join(lines) + "\n")
        print("[ok] README metadata updated")
    else:
        print("[ok] No README metadata changes needed")

    return 0


if __name__ == "__main__":
    sys.exit(main())
