"""CODEOWNERS parsing.

Each non-blank, non-comment line is: <path pattern> <owner> <owner> ...
Owners are @user, @org/team, or an email address. A pattern with no owners is valid
(it means "no owner" for those paths). `#` starts a comment unless escaped as `\\#`.
"""

import re
from dataclasses import dataclass

# GitHub's lookup order on the default branch; the first one found wins.
CODEOWNERS_PATHS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")

_UNESCAPED_HASH = re.compile(r"(?<!\\)#")


@dataclass(frozen=True)
class CodeownersRule:
    line_number: int
    pattern: str
    owners: list[str]


def parse_codeowners(text: str) -> list[CodeownersRule]:
    rules: list[CodeownersRule] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = _UNESCAPED_HASH.split(raw_line, maxsplit=1)[0].strip()
        if not line:
            continue
        pattern, *owners = line.split()
        rules.append(CodeownersRule(line_number, pattern.replace("\\#", "#"), owners))
    return rules
