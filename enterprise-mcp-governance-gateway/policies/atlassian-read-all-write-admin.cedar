// Real target: Atlassian (Jira + Confluence) MCP server, fronted by the gateway
// with per-user Atlassian OAuth (3LO). Tools surface as Atlassian___<tool>.
//
// Governance model:
//   * READ/SEARCH  -> permitted for every authenticated gateway user.
//   * WRITE        -> permitted ONLY for users carrying role = "atlassian-writer".
//     Demo users have no `role` tag in the Cognito ACCESS token, so the write
//     permits never fire and writes stay DENIED by default (the safe MVP posture).
//     To enable writes in production, surface `custom:role` into the access token
//     (Cognito pre-token-generation Lambda) — then these permits enforce as written.
//
// NOTE: one single-action statement per tool — the policy engine does not match
// Cedar action set-membership (`action in [...]`). Resource ARN is substituted at
// deploy time (token __GATEWAY_ARN__). Beneath this, Atlassian still enforces each
// user's own product permissions (the 3LO token is theirs) — defence in depth.

// ---- Jira read (all authenticated users) ----
permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"Atlassian___getJiraIssue",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
);

permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"Atlassian___searchJiraIssuesUsingJql",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
);

permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"Atlassian___getVisibleJiraProjects",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
);

// ---- Confluence read (all authenticated users) ----
permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"Atlassian___getConfluencePage",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
);

permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"Atlassian___searchConfluenceUsingCql",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
);

permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"Atlassian___getConfluenceSpaces",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
);

// ---- Jira write (role = "atlassian-writer" only) ----
permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"Atlassian___createJiraIssue",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
)
when { principal.hasTag("role") && principal.getTag("role") == "atlassian-writer" };

permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"Atlassian___editJiraIssue",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
)
when { principal.hasTag("role") && principal.getTag("role") == "atlassian-writer" };

permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"Atlassian___addCommentToJiraIssue",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
)
when { principal.hasTag("role") && principal.getTag("role") == "atlassian-writer" };

permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"Atlassian___transitionJiraIssue",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
)
when { principal.hasTag("role") && principal.getTag("role") == "atlassian-writer" };

// ---- Confluence write (role = "atlassian-writer" only) ----
permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"Atlassian___createConfluencePage",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
)
when { principal.hasTag("role") && principal.getTag("role") == "atlassian-writer" };

permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"Atlassian___updateConfluencePage",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
)
when { principal.hasTag("role") && principal.getTag("role") == "atlassian-writer" };
