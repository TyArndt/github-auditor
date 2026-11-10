"""GraphQL queries. Each includes `rateLimit { cost }` so the client can track points.

GraphQL is used where one call can replace many REST calls (repos with CODEOWNERS,
branches with their matching classic rule, teams with members, org members with role).
"""

ORG_REPOS = """
query OrgRepos($org: String!, $cursor: String) {
  rateLimit { cost }
  organization(login: $org) {
    repositories(first: 50, after: $cursor, orderBy: {field: NAME, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        databaseId
        id
        name
        nameWithOwner
        description
        visibility
        isArchived
        isDisabled
        isFork
        isTemplate
        isEmpty
        diskUsage
        createdAt
        updatedAt
        pushedAt
        primaryLanguage { name }
        defaultBranchRef { name }
        repositoryTopics(first: 25) { nodes { topic { name } } }
        # GitHub's CODEOWNERS lookup order on the default branch (HEAD).
        codeownersGithub: object(expression: "HEAD:.github/CODEOWNERS") { ... on Blob { oid text } }
        codeownersRoot: object(expression: "HEAD:CODEOWNERS") { ... on Blob { oid text } }
        codeownersDocs: object(expression: "HEAD:docs/CODEOWNERS") { ... on Blob { oid text } }
      }
    }
  }
}
"""

# Branches plus every classic protection rule, in one query per 100 branches.
# Ref.branchProtectionRule is the classic rule GitHub itself applies to that branch.
REPO_BRANCHES = """
query RepoBranches($owner: String!, $name: String!, $cursor: String) {
  rateLimit { cost }
  repository(owner: $owner, name: $name) {
    branchProtectionRules(first: 100) {
      nodes {
        databaseId
        id
        pattern
        requiresApprovingReviews
        requiredApprovingReviewCount
        dismissesStaleReviews
        requiresCodeOwnerReviews
        requireLastPushApproval
        requiresStatusChecks
        requiresStrictStatusChecks
        requiredStatusCheckContexts
        requiresConversationResolution
        requiresLinearHistory
        requiresCommitSignatures
        isAdminEnforced
        allowsForcePushes
        allowsDeletions
        blocksCreations
        lockBranch
      }
    }
    refs(refPrefix: "refs/heads/", first: 100, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        name
        branchProtectionRule { databaseId }
        target {
          ... on Commit {
            oid
            committedDate
            author { name user { login } }
          }
        }
      }
    }
  }
}
"""

# Kept out of ORG_REPOS on purpose: `pullRequests` needs the Pull requests: read permission,
# and a permission error would fail the whole repository listing. `totalCount` alone
# fetches no PR nodes.
ORG_REPO_OPEN_PRS = """
query OrgRepoOpenPrs($org: String!, $cursor: String) {
  rateLimit { cost }
  organization(login: $org) {
    repositories(first: 100, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        databaseId
        pullRequests(states: OPEN) { totalCount }
      }
    }
  }
}
"""

ORG_MEMBERS = """
query OrgMembers($org: String!, $cursor: String) {
  rateLimit { cost }
  organization(login: $org) {
    membersWithRole(first: 100, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      edges { role node { databaseId id login name } }
    }
  }
}
"""

# IMMEDIATE membership only: inheritance through nested teams is computed in SQL views.
ORG_TEAMS = """
query OrgTeams($org: String!, $cursor: String) {
  rateLimit { cost }
  organization(login: $org) {
    teams(first: 50, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        databaseId
        id
        slug
        name
        description
        privacy
        parentTeam { databaseId }
        members(first: 100, membership: IMMEDIATE) {
          pageInfo { hasNextPage endCursor }
          edges { role node { databaseId id login name } }
        }
      }
    }
  }
}
"""

TEAM_MEMBERS = """
query TeamMembers($org: String!, $slug: String!, $cursor: String) {
  rateLimit { cost }
  organization(login: $org) {
    team(slug: $slug) {
      members(first: 100, after: $cursor, membership: IMMEDIATE) {
        pageInfo { hasNextPage endCursor }
        edges { role node { databaseId id login name } }
      }
    }
  }
}
"""
