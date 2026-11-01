# Implementation Guide — GitHub Repository Inventory & Auditor

| | |
|---|---|
| Status | Draft v0.1. Sections fill in as each phase is delivered |
| Last updated | 2026-11-01 |
| Related | [solution-design.md](solution-design.md), [architecture.md](architecture.md) |

This guide is the hands-on companion to the design docs: how to set up, run, deploy, and operate the system. Sections marked **(Phase N — pending)** get filled in when that phase is built.

---

## 1. Prerequisites

| Tool | Version | Install (Windows) | Needed from |
|---|---|---|---|
| Python | 3.14 | python.org installer (`py -3.14`) | Phase 0 |
| Docker Desktop | latest | docker.com | Phase 2 |
| Git | latest | git-scm.com | Phase 0 |
| Terraform | ≥ 1.9 | `winget install Hashicorp.Terraform` | Phase 4 |
| AWS CLI v2 | latest | `winget install Amazon.AWSCLI` | Phase 4 |
| Node.js | LTS | nodejs.org | Phase 6 |

Access needed:
- **GitHub org owner** (or an enterprise owner) to create and install the GitHub App.
- An **AWS account** with permission to create VPC, RDS, ECS, ECR, Secrets Manager, and IAM resources.

---

## 2. Local development setup

```powershell
git clone <this repo>
cd github_auditor
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1                      # all later commands assume the venv is active
python.exe  -m pip install --require-hashes -r requirements-dev.txt
pip install --no-deps -e .
copy .env.example .env       # then fill in values (see §3.4)
docker compose up -d db      # local PostgreSQL 17
auditor check-config         # validates configuration
.\scripts\check.ps1          # same checks as CI: ruff, format, mypy, pytest
```

Optional: run `git config core.hooksPath .githooks` once per clone to run lint, format, and type checks before every commit. This is a plain git hook; the `pre-commit` package isn't used because it depends on `virtualenv`, which is blocked.

### 2.1 Dependency management
Versions are never hand-written. `pyproject.toml` lists package **names** only. `pip-compile` resolves the latest versions from PyPI into `requirements.txt` (runtime, used by the container) and `requirements-dev.txt` (dev/CI), with SHA-256 hashes. `pip-audit` then checks both files against known vulnerabilities.

| Task | Command |
|---|---|
| Update everything to latest and scan | `.\scripts\lock.ps1` |
| Sync your venv to the lock files | `pip-sync requirements-dev.txt; pip install --no-deps -e .` |
| Add a dependency | Add the name to `pyproject.toml`, then run `.\scripts\lock.ps1` |
| A package fails the Sonatype scan | Add it to `blocked-packages.txt` with the reason, remove whatever pulls it in (find it under `# via` in the requirements file), run `.\scripts\lock.ps1` |

**Blocked packages:** `blocked-packages.txt` lists packages our Sonatype policy rejects (currently SQLAlchemy, Alembic, virtualenv). `lock.ps1` and CI fail if any of them appears in a lock file, even as a dependency of a dependency.

CI runs `pip-audit` on every PR and weekly. Dependabot opens weekly PRs for Python packages and GitHub Actions. Actions are pinned to commit SHAs.

### 2.2 Corporate SSL inspection (TLS decryption)
Our network decrypts and re-signs HTTPS traffic with a company root CA. Anything that makes HTTPS calls must trust that root, or it fails with `CERTIFICATE_VERIFY_FAILED`. Get the company CA bundle (PEM format) from IT and store it **outside the repo**, e.g. `C:\Users\you\.secrets\corp-ca-bundle.pem`. Certificate files (`*.pem`, `*.crt`, `*.cer`) are gitignored.

| Tool | How it trusts the bundle |
|---|---|
| **auditor** (`whoami`, `sync`) | `CA_BUNDLE=C:\...\corp-ca-bundle.pem` in `.env`. It is trusted **in addition to** the public CAs, so hosts excluded from inspection keep working. TLS verification is never turned off. `auditor check-config` shows the active bundle |
| **pip** (installs, `pip-sync`, `pip-compile`) | Once per machine: `python -m pip config set global.cert C:\...\corp-ca-bundle.pem` |
| **pip-audit** (in `lock.ps1`) | `$env:REQUESTS_CA_BUNDLE = "C:\...\corp-ca-bundle.pem"` before running `.\scripts\lock.ps1` (pip-audit uses `requests`, which reads this variable) |

> Note: `pip config set global.cert` and `REQUESTS_CA_BUNDLE` make those tools trust **only** that file. If installs then fail for hosts that aren't inspected, ask IT for a bundle that also contains the public roots, or append the contents of `python -m certifi` (the path to the public bundle) to a copy of the corporate one.

**In AWS (Phase 4):** traffic from ECS to GitHub normally leaves through a NAT gateway and is *not* inspected, so leave `CA_BUNDLE` unset there. If your AWS egress is routed through an inspecting firewall, set `CA_BUNDLE` in the task and include the bundle in the image or mount it.

---

## 3. Phase 1 — Create the GitHub App

### 3.1 Create the App
1. Go to **`https://github.com/organizations/<ORG>/settings/apps`** → **New GitHub App**.
2. Fill in:
   - **GitHub App name:** `<org>-repo-auditor` (must be globally unique)
   - **Homepage URL:** your internal wiki or this repo's URL
   - **Webhook:** **uncheck "Active"**. We don't use webhooks in v1.
   - **Callback URL / user authorization:** leave empty. We don't act on behalf of users.
3. **Repository permissions** (all **Read-only**):
   - Actions
   - Administration
   - Contents
   - Custom properties
   - Metadata (mandatory, set automatically)
4. **Organization permissions** (all **Read-only**):
   - Administration
   - Members
   - Custom properties (optional, future-proofing)
5. **Where can this GitHub App be installed?** → **Only on this account**.
6. Click **Create GitHub App**.

### 3.2 Generate the private key
1. On the App's settings page, note the **App ID** shown at the top.
2. Scroll to **Private keys** → **Generate a private key**. A `.pem` file downloads.
3. Treat it like a password:
   - Locally, store it **outside the repo** (e.g. `C:\Users\<you>\.secrets\repo-auditor.pem`).
   - In AWS, it goes into Secrets Manager (§6). Never commit it, email it, or paste it into chat.

### 3.3 Install the App on the org
1. App settings → **Install App** → choose the org → **All repositories** → **Install**.
2. The browser lands on `https://github.com/organizations/<ORG>/settings/installations/<INSTALLATION_ID>`. The number at the end is the **Installation ID**.

> Choose **All repositories**, not a selection. New repos are then covered automatically.

### 3.4 Configure locally
Edit `.env`:
```dotenv
GITHUB_ORG=your-org
GITHUB_APP_ID=123456
GITHUB_INSTALLATION_ID=78901234
GITHUB_PRIVATE_KEY_PATH=C:\Users\you\.secrets\repo-auditor.pem
CA_BUNDLE=C:\Users\you\.secrets\corp-ca-bundle.pem   # SSL inspection, see §2.2
DATABASE_URL=postgresql://auditor:auditor@localhost:5432/auditor
```

### 3.5 Verify
```powershell
auditor whoami
```
Example output:
```
app:              Acme Repo Auditor (acme-repo-auditor)
installed on:     acme (all repositories)
repos visible:    2534
org:              acme (base permission: read)
rate limit:       core 14990/15000 per hour
rate limit:       graphql 10000/10000 per hour
permissions:      actions=read, administration=read, contents=read, members=read, ...
GitHub App OK
```
It exits with an error and a `PROBLEM:` line for each of these:
- a required permission is missing (see §3.1)
- the App is installed on *selected* repositories instead of all of them
- the installation belongs to a different org than `GITHUB_ORG`
- the org base permission can't be read (needs Organization → Administration: read)

### 3.6 Rotating the private key
1. Generate a new key on the App page; GitHub allows two keys to be active at once.
2. Update the secret in Secrets Manager (and your local file).
3. Run a manual sync to confirm it works.
4. Delete the old key on the App page.

---

## 4. Phase 2 — Database
Design and rationale: **[database-design.md](database-design.md)**.

```powershell
docker compose up -d db          # local PostgreSQL 17
auditor db migrate               # apply pending db/migrations/*.sql + rebuild views in `reporting`
auditor db status                # list applied / pending migrations (exit code 2 if pending)
pytest tests/db                  # view + migration tests (creates and drops a throwaway database)
docker compose exec db psql -U auditor -d auditor   # poke around: \dt inventory.*  \dv reporting.*
```

| Task | How |
|---|---|
| Change a table | Add `db/migrations/NNNN_description.sql` (next number) → `auditor db migrate`. **Never edit an applied migration**; the runner refuses changed checksums |
| Change a report view | Edit `db/views/NN_name.sql` → `auditor db migrate` (no migration needed) |
| Reset local DB | `docker compose down -v; docker compose up -d db; auditor db migrate` |

Roles: `auditor` owns everything (collector and migrations). `auditor_read` is a NOLOGIN group with `SELECT` on `reporting.*` only. Report tools log in as a user that is a member of it (created in Phase 4).

## 5. Phase 3 — Running the collector

```powershell
auditor db migrate          # make sure the schema is current
auditor whoami              # App OK?
auditor sync --limit 50     # trial: first 50 repos (by name)
auditor sync                # full run (all repos)
```

| Mode | What it does | What it never does |
|---|---|---|
| `--limit N` | Reads org, members, teams, and the first N repos in full | Soft-delete repos, team grants, rulesets, or users (only part of the org was read) |
| full | Reads everything; marks vanished repos/branches/grants as deleted; writes today's snapshot | Delete org-wide things (rulesets, users) if any repo or team failed |

**Exit codes:** `0` success · `2` partial (some repos failed; see the errors printed, or `sync_runs.errors`) · `1` crash.

**Check the result:**
```sql
SELECT * FROM reporting.v_sync_runs ORDER BY sync_run_id DESC LIMIT 5;  -- status, duration, API use
SELECT repository, failures FROM reporting.v_default_branch_compliance WHERE NOT is_compliant;
```

**API budget:** the log's last line reports REST calls (and how many were free `304 Not Modified` responses) plus GraphQL points. The first run is the most expensive; later runs reuse ETags from `inventory.http_cache`. On the first real run, compare these numbers to the estimate in architecture.md §3.2.

**Validate against GitHub (first real run):** pick 5–10 repos and compare `v_branch_protection` with each repo's **Settings → Rules / Branches** page, and `v_repo_access` with **Settings → Collaborators and teams**. The ruleset matching in `src/auditor/protection.py` is our own implementation of GitHub's patterns, so this check matters.

## 6. Phase 4 — AWS deployment **(pending)**
- Terraform bootstrap (S3 state bucket)
- `terraform apply` per environment
- Loading secrets into Secrets Manager
- Triggering a manual run: `aws ecs run-task ...` / Scheduler "run now"

## 7. Phase 5 — Metabase **(pending)**
- First-time setup, connecting with the read-only role
- Importing the dashboard definitions

## 8. Phase 6 — Web app **(pending)**

## 9. Phase 7 — Actions metrics **(pending)**

---

## 10. Runbooks **(filled in during Phases 4–8)**

| Situation | Steps |
|---|---|
| Daily run failed | Check the CloudWatch log for the task → `sync_runs.errors` → re-run manually |
| Rate-limited | Confirm no other process uses the App; lower `COLLECTOR_CONCURRENCY`; re-run |
| App key rotation | §3.6 |
| Repo shows as deleted but isn't | Check that the App is installed on **all** repos; check the run status was `success` |
| Restore DB | RDS point-in-time restore → repoint the secret |

## 11. Extending: adding a new data point **(pattern, finalized in Phase 3)**
1. Add the field to the GraphQL query or REST call in `src/auditor/collect/<domain>.py`.
2. Add a column with a new migration file (`db/migrations/NNNN_add_x.sql`: `ALTER TABLE ... ADD COLUMN ...`).
3. Map it in the loader.
4. Expose it in the relevant view under `db/views/`.
5. Add a test with a recorded API response.
