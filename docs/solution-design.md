# Solution Design — GitHub Repository Inventory & Auditor

| | |
|---|---|
| Status | Draft v0.1 |
| Last updated | 2026-11-01 |
| Related | [architecture.md](architecture.md), [implementation-guide.md](implementation-guide.md) |

## 1. Problem

The organization runs on GitHub Enterprise Cloud with Enterprise Managed Users (Okta). It has thousands of repositories and many more branches. Nobody can easily answer basic governance questions:

- Which repositories exist? Are they active, stale, or archived?
- Who has access to each repository, at what level, and why?
- Who is listed as a code owner?
- Does the repository use GitHub Actions, and when did a workflow last run?
- Which branches exist, and which are protected? Does the default branch meet our minimum protection standard?

GitHub's UI answers these one repository at a time. We need an org-wide, queryable, historical view.

## 2. Goals and non-goals

### Goals
1. Keep a complete daily **inventory** of repositories, branches, branch protection, workflows, access, and code owners.
2. Store it in **PostgreSQL** with a clean relational model that keeps **history**: when something appeared, changed, or was deleted.
3. Provide **reports**: first in Metabase, later in a custom web app.
4. Use **read-only** access through a dedicated GitHub App, with no personal tokens.
5. Finish a full daily refresh in **under 1 hour** within GitHub API rate limits.
6. Be extensible: GitHub Actions metrics, webhooks, and other data can be added without a redesign.

### Non-goals (v1)
- Changing anything in GitHub, such as auto-remediating protection. The system is read-only.
- Linking GitHub users to Okta/HR records. This is possible later from an Okta export.
- Real-time freshness. Daily is enough, and webhooks are a later option.
- Scanning code content, secrets, or dependencies. GitHub Advanced Security covers that.

## 3. Users and use cases

| User | Need |
|---|---|
| Platform / DevOps team | Inventory, stale repo cleanup, Actions adoption |
| Security / compliance | Branch protection compliance, access reviews, admin sprawl |
| Engineering managers | "What do my teams own and who can push to it?" |
| Auditors | Point-in-time evidence: protection status and access as of a date |

## 4. Scope: tracked data

### 4.1 Repository
Name, full name, visibility (`private`/`internal`; EMU orgs have no public repos), archived, fork, template, default branch, primary language, topics, created/updated/pushed timestamps, size, description, custom properties (captured generically in case they're added later), and whether the repo is disabled.

Also refreshed on every sync: the number of **open pull requests** and the number of **open Dependabot alerts**, broken down by severity (critical/high/medium/low). Both are optional: if the GitHub App lacks the Pull requests or Dependabot alerts read permission, the counts are left empty (unknown) rather than reported as zero. A repo with Dependabot alerts turned off shows 0 open alerts.

### 4.2 Branches
For every branch: name, head commit SHA, last commit date and author, whether it's the default branch, whether it's protected, and *what* protects it (classic rule, ruleset, both, or none).

### 4.3 Branch protection
- **Classic branch protection rules**: pattern, required approving reviews, dismiss stale reviews, require code owner review, required status checks, enforce for admins, allow force pushes, allow deletions, linear history, signed commits.
- **Repository rulesets** and **organization rulesets** that target the repo: name, enforcement (`active`/`evaluate`/`disabled`), target conditions, rules, and bypass actors.

### 4.4 Access ("ownership")
"Owner" means **who has permission on the repository**. GitHub grants access through four sources, and we record each one separately so the report can explain *why* someone has access:

| Source | Example | How it's collected |
|---|---|---|
| Org base permission | Every member has `read` | Org settings (one value) |
| Org owner | Org admins implicitly have `admin` on every repo | Org members with role `admin` |
| Team grant | `@acme/payments` has `write` | Team → repo permissions, plus team membership and nested teams |
| Direct collaborator | `jdoe_acme` granted `maintain` | Repo collaborators with affiliation `direct` |

Permission levels are `read` < `triage` < `write` < `maintain` < `admin`. Custom repository roles are stored with their underlying base role so they can be ranked. **Effective access** for a user is the highest level across all sources.

> Org base permission and org owners would add one row for *every member × every repo* (potentially millions of rows). They are stored once and applied in reports, not expanded into tables. See [architecture.md §5.3](architecture.md#53-access-model).

### 4.5 Code owners
The CODEOWNERS file on the default branch, looked up in GitHub's order: `.github/CODEOWNERS`, then `CODEOWNERS`, then `docs/CODEOWNERS`. We store the file location and every rule (path pattern → owners). Owners can be users, teams (`@org/team`), or emails. A repo-level summary lists the distinct owning teams and users.

### 4.6 GitHub Actions
Each workflow's name, path, and state (active/disabled). Also the most recent run's date, status, and conclusion, and the number of runs in the last 30 days. Detailed run metrics (durations, success rates, minutes) are **Phase 7**.

### 4.7 Run audit
Every collector run records its start/end time, status, counts, API calls used, and per-repo errors.

## 5. Compliance rule (v1)

A repository's **default branch is compliant** when the protections in force, combined across classic rules and **active** rulesets, include all of the following:

| Requirement | Classic rule field | Ruleset rule |
|---|---|---|
| ≥ 1 required approving review | `requiredApprovingReviewCount ≥ 1` | `pull_request.required_approving_review_count ≥ 1` |
| Force pushes blocked | `allowsForcePushes = false` | `non_fast_forward` |
| Deletion blocked | `allowsDeletions = false` | `deletion` |

Rulesets in `evaluate` or `disabled` mode do **not** count. Archived repositories are reported separately and excluded from the compliance percentage. Items such as admin bypass, `enforce_admins`, and ruleset bypass actors are shown as **informational**, without failing compliance, in v1. The rule lives in one SQL view, so it's easy to tighten later.

## 6. Reports (v1 catalog)

| # | Report | Key questions |
|---|---|---|
| R1 | Repository inventory | All repos with visibility, archived, language, last push, branch count, has workflows, code owner teams |
| R2 | Stale repositories | No push in more than 90/180/365 days and not archived; archive candidates |
| R3 | Actions usage | Repos with/without workflows, last workflow run, workflows that never ran |
| R4 | Branch inventory | Branches per repo, stale branches (no commit in more than 90 days), branch counts |
| R5 | Branch protection | Every branch: protected? by what? Default-branch protection details |
| R6 | **Default-branch compliance** | Compliant/non-compliant per §5, and which requirement fails |
| R7 | Repository access | Per repo: who has access, effective level, and source (team/direct/org) |
| R8 | Admin sprawl | Repos with many admins, users who are admin on many repos, direct admin grants |
| R9 | Code owners | Repos missing CODEOWNERS, owners per repo, teams by number of owned repos |
| R10 | Trends | Compliance %, repo count, and Actions adoption over time (daily snapshots) |
| R11 | Collector health | Last successful run, duration, errors |

## 7. Non-functional requirements

| Area | Requirement |
|---|---|
| Freshness | Data no more than 24 hours old; daily run at 02:00 UTC |
| Runtime | Full sync under 60 minutes; daily incremental target under 20 minutes |
| API budget | Stay under 50% of the 15,000/hr installation REST limit, honor secondary rate limits |
| Scale | Large organizations (thousands to tens of thousands of repos) without a redesign; API cost grows linearly per repo (architecture §3.2) |
| Security | Read-only App permissions; private key in AWS Secrets Manager; DB in private subnets; no public endpoints until Okta auth is added |
| Auditability | Soft deletes and daily snapshots give point-in-time answers; `sync_runs` logs every run |
| Retention | Current state kept indefinitely; daily snapshots 3 years; Actions runs 13 months (Phase 7) |
| Reliability | One bad repo never fails the whole run; failed or late runs raise an alarm |

## 8. Constraints and assumptions

- GitHub Enterprise Cloud with EMU. User logins carry the enterprise shortcode suffix (e.g. `jdoe_acme`). There are no outside collaborators.
- A single organization for v1. The schema supports more than one.
- The App installation can read org settings, teams, and rulesets. This is confirmed in Phase 1.
- Okta SSO for the reporting UI comes last. Until then, reporting is internal-network only, and Metabase uses its own built-in logins.

## 9. Delivery phases

| Phase | Outcome |
|---|---|
| 0 | Design docs, repo skeleton (this doc) |
| 1 | GitHub App created and installed; auth module; `auditor whoami` |
| 2 | Database schema, migrations, report views |
| 3 | Collector running locally against the real org |
| 4 | AWS infrastructure (Terraform), daily scheduled run |
| 5 | Metabase dashboards (R1–R11) |
| 6 | Custom web app (FastAPI + React) |
| 7 | GitHub Actions metrics, optional webhooks |
| 8 | Okta SSO, hardening, runbooks |

## 10. Risks

| Risk | Mitigation |
|---|---|
| Rate limits during the first full load | GraphQL bulk queries, ETags, bounded concurrency, resumable runs |
| Org base permission or org owners make access tables explode | Store once, apply in views (§4.4) |
| Ruleset matching logic differs from GitHub's | Spot-check against GitHub's "rules for branch" API in tests and in a sampling job |
| App lacks a permission for some field (e.g. org base permission) | Verify in Phase 1; degrade gracefully and flag the field as unknown |
| Large repos with thousands of branches | Paginate per repo, apply time limits, and isolate errors |
