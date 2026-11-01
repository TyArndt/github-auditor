"""Which rulesets target which branch.

A branch-targeting ruleset has conditions like:
    {"ref_name": {"include": ["~DEFAULT_BRANCH", "refs/heads/release/*"], "exclude": []}}

Rules (GitHub's fnmatch semantics, File::FNM_PATHNAME):
    ~DEFAULT_BRANCH   the repository's default branch
    ~ALL              every branch
    *                 any characters except "/"
    **                any characters including "/"
    ?                 one character except "/"
    [abc]             character class
A branch matches when it matches any include and no exclude. A ruleset with no
include patterns targets nothing (we under-report rather than over-report protection).

Repository/org-level scoping (which REPOS a ruleset applies to) is resolved by GitHub:
the repo's ruleset listing with includes_parents=true only returns rulesets for that repo.
"""

import re
from functools import lru_cache
from typing import Any

_HEADS = "refs/heads/"


@lru_cache(maxsize=1024)
def _pattern_regex(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            end = pattern.find("]", i + 1)
            if end == -1:
                out.append(re.escape(char))
            else:
                body = pattern[i + 1 : end]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = end
        else:
            out.append(re.escape(char))
        i += 1
    return re.compile("".join(out) + r"\Z")


def ref_matches(pattern: str, branch: str, default_branch: str | None) -> bool:
    if pattern == "~ALL":
        return True
    if pattern == "~DEFAULT_BRANCH":
        return default_branch is not None and branch == default_branch
    return _pattern_regex(pattern).match(_HEADS + branch) is not None


def ruleset_targets_branch(
    ruleset: dict[str, Any], branch: str, default_branch: str | None
) -> bool:
    """True if this ruleset's branch conditions select `branch` (enforcement not considered)."""
    if ruleset.get("target", "branch") != "branch":
        return False
    ref_name = (ruleset.get("conditions") or {}).get("ref_name") or {}
    include = ref_name.get("include") or []
    exclude = ref_name.get("exclude") or []
    return any(ref_matches(p, branch, default_branch) for p in include) and not any(
        ref_matches(p, branch, default_branch) for p in exclude
    )
