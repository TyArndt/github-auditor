import pytest

from auditor.codeowners import parse_codeowners
from auditor.protection import ref_matches, ruleset_targets_branch


@pytest.mark.parametrize(
    ("pattern", "branch", "expected"),
    [
        ("~ALL", "anything/at/all", True),
        ("~DEFAULT_BRANCH", "main", True),
        ("~DEFAULT_BRANCH", "develop", False),
        ("refs/heads/main", "main", True),
        ("refs/heads/main", "main2", False),
        ("refs/heads/release/*", "release/1.0", True),
        ("refs/heads/release/*", "release/1.0/hotfix", False),  # * stops at "/"
        ("refs/heads/release/**", "release/1.0/hotfix", True),  # ** crosses "/"
        ("refs/heads/v?", "v1", True),
        ("refs/heads/v?", "v10", False),
        ("refs/heads/[dm]*", "dev", True),
        ("refs/heads/[!dm]*", "dev", False),
        ("refs/heads/feat.x", "featYx", False),  # "." is literal, not regex
    ],
)
def test_ref_patterns(pattern: str, branch: str, expected: bool) -> None:
    assert ref_matches(pattern, branch, default_branch="main") is expected


def test_ruleset_include_and_exclude() -> None:
    ruleset = {
        "target": "branch",
        "conditions": {"ref_name": {"include": ["~ALL"], "exclude": ["refs/heads/sandbox/**"]}},
    }
    assert ruleset_targets_branch(ruleset, "main", "main")
    assert not ruleset_targets_branch(ruleset, "sandbox/try", "main")


def test_ruleset_without_include_or_for_tags_targets_nothing() -> None:
    assert not ruleset_targets_branch({"target": "branch", "conditions": {}}, "main", "main")
    tag_rule = {"target": "tag", "conditions": {"ref_name": {"include": ["~ALL"]}}}
    assert not ruleset_targets_branch(tag_rule, "main", "main")


def test_default_branch_token_on_empty_repo() -> None:
    assert not ref_matches("~DEFAULT_BRANCH", "main", default_branch=None)


def test_codeowners_parsing() -> None:
    text = "\n".join(
        [
            "# Global owners",
            "*       @acme/platform @alice_acme",
            "",
            "/docs/  docs@acme.com   # trailing comment",
            "/legal/ ",
            r"/weird\#name  @bob_acme",
        ]
    )
    rules = [(r.line_number, r.pattern, r.owners) for r in parse_codeowners(text)]
    assert rules == [
        (2, "*", ["@acme/platform", "@alice_acme"]),
        (4, "/docs/", ["docs@acme.com"]),
        (5, "/legal/", []),  # explicitly unowned
        (6, "/weird#name", ["@bob_acme"]),
    ]
