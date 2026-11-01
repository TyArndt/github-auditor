"""One collector run: GitHub -> records -> database.

Order (each numbered step is its own transaction):
  1. org, org members (owners), custom roles
  2. repositories (+ CODEOWNERS, custom properties), org rulesets
  3. teams, team members, team -> repo grants
  4. per repo, in worker threads: branches + classic rules, rulesets, collaborators,
     workflows — each repo loaded in its own transaction as soon as it's fetched
  5. soft-delete what disappeared (only for scopes read completely), daily snapshot
One failing repo or team is recorded in sync_runs.errors and the run continues
(status 'partial'). The run row is always closed, even on a crash.
"""

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

import psycopg

from auditor import __version__
from auditor.collect import (
    RulesetFetcher,
    fetch_custom_properties,
    fetch_custom_roles,
    fetch_org,
    fetch_org_members,
    fetch_repo_details,
    fetch_repos,
    fetch_team_grants,
    fetch_teams,
)
from auditor.config import Settings
from auditor.db.migrate import to_psycopg_url
from auditor.github.auth import AppAuth
from auditor.github.cache import EtagCache
from auditor.github.client import GitHubClient, make_http
from auditor.load import Loader, finish_run, start_run

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    run_id: int
    status: str
    repos_seen: int = 0
    repos_deep_synced: int = 0
    branches_seen: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)


def build_client(settings: Settings, etags: EtagCache | None = None) -> GitHubClient:
    http = make_http(settings.github_api_url, settings.github_api_version)
    auth = AppAuth(
        settings.github_app_id,
        settings.github_installation_id,
        settings.private_key_pem(),
        http,
    )
    return GitHubClient(auth, http, etags, rate_limit_floor=settings.rate_limit_floor)


def _error(stage: str, exc: BaseException, **context: Any) -> dict[str, Any]:
    return {"stage": stage, **context, "error": f"{type(exc).__name__}: {exc}"[:1000]}


def run_sync(
    settings: Settings,
    *,
    limit: int | None = None,
    client_factory: Callable[[EtagCache], GitHubClient] | None = None,
) -> SyncResult:
    org_login = settings.github_org
    etags = EtagCache()
    url = to_psycopg_url(settings.database_url.get_secret_value())

    with psycopg.connect(url, autocommit=True) as conn:
        etags.load(conn)
        client = (client_factory or (lambda cache: build_client(settings, cache)))(etags)
        run = start_run(conn, mode="full", app_version=__version__)
        result = SyncResult(run_id=run.id, status="running")
        loader = Loader(conn, run, partial=limit is not None)
        log.info("sync run %s started (org=%s, limit=%s)", run.id, org_login, limit)

        try:
            _sync(conn, client, loader, org_login, limit, settings, result)
            result.status = "success" if not result.errors else "partial"
        except Exception as exc:
            result.status = "failed"
            result.errors.append(_error("run", exc))
            log.exception("sync run %s failed", run.id)
            raise
        finally:
            etags.flush(conn)
            finish_run(
                conn,
                run,
                result.status,
                result.errors,
                repos_seen=result.repos_seen,
                repos_deep_synced=result.repos_deep_synced,
                branches_seen=result.branches_seen,
                rest_calls=client.stats.rest_calls,
                graphql_points=client.stats.graphql_points,
            )
            log.info(
                "sync run %s %s: %d repos (%d detailed), %d branches, %d REST calls "
                "(%d not modified), %d GraphQL points, %d errors",
                run.id,
                result.status,
                result.repos_seen,
                result.repos_deep_synced,
                result.branches_seen,
                client.stats.rest_calls,
                client.stats.rest_not_modified,
                client.stats.graphql_points,
                len(result.errors),
            )
    return result


def _sync(
    conn: psycopg.Connection[Any],
    client: GitHubClient,
    loader: Loader,
    org_login: str,
    limit: int | None,
    settings: Settings,
    result: SyncResult,
) -> None:
    # 1. organization
    org = fetch_org(client, org_login)
    members = fetch_org_members(client, org_login)
    roles = fetch_custom_roles(client, org_login)
    with conn.transaction():
        loader.org(org)
        loader.org_members(members)
        if roles is not None:
            loader.custom_roles(roles)

    # 2. repositories and org-level rulesets
    repos = fetch_repos(client, org_login, limit)
    properties = fetch_custom_properties(client, org_login) or {}
    for repo in repos:
        repo.custom_properties = properties.get(repo.id, {})
    result.repos_seen = len(repos)
    rulesets = RulesetFetcher(client, org_login)
    org_rulesets = rulesets.org_rulesets()
    with conn.transaction():
        loader.repos(repos)
        loader.rulesets(org_rulesets)
    log.info("%d repositories, %d org rulesets", len(repos), len(org_rulesets))

    repo_ids = {r.id for r in repos}
    with ThreadPoolExecutor(max_workers=settings.collector_concurrency) as pool:
        # 3. teams
        teams = fetch_teams(client, org_login)
        with conn.transaction():
            loader.teams(teams)
            for team in teams:
                loader.team_members(team)
        grant_futures = {pool.submit(fetch_team_grants, client, org_login, t): t for t in teams}
        for grant_future in as_completed(grant_futures):
            team = grant_futures[grant_future]
            try:
                grants = grant_future.result()
                with conn.transaction():
                    loader.team_grants(team.id, grants, repo_ids)
            except Exception as exc:
                result.errors.append(_error("team_grants", exc, team=team.slug))
        log.info("%d teams", len(teams))

        # 4. per-repository details
        detail_futures = {pool.submit(fetch_repo_details, client, r, rulesets): r for r in repos}
        for done, detail_future in enumerate(as_completed(detail_futures), start=1):
            repo = detail_futures[detail_future]
            try:
                details = detail_future.result()
                with conn.transaction():
                    loader.repo_details(details)
                result.repos_deep_synced += 1
                result.branches_seen += len(details.branches)
            except Exception as exc:
                result.errors.append(_error("repo", exc, repo=repo.full_name))
            if done % 100 == 0 or done == len(repos):
                log.info("repos %d/%d (%d errors)", done, len(repos), len(result.errors))

    # 5. deletions and snapshot
    with conn.transaction():
        if limit is None:  # the full repo listing succeeded, so absent repos are gone
            loader.soft_delete_repos()
            if not result.errors:  # every repo/team read cleanly
                loader.soft_delete_rulesets()
                loader.soft_delete_users()
            loader.snapshot()
