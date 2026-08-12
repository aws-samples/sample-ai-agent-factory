// Real target: GitHub remote MCP server.
// Everyone may read/search; writes forbidden unless the user carries the
// "github-writer" role tag.
// Resource ARN is substituted at deploy time (token __GATEWAY_ARN__).

// One single-action statement per tool: the policy engine does not match Cedar
// action set-membership (`action in [...]`) for tool authorization.

// Everyone may read/search GitHub.
permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"GitHub___search_repositories",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
);

permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"GitHub___search_code",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
);

permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"GitHub___search_issues",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
);

permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"GitHub___get_file_contents",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
);

permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"GitHub___list_pull_requests",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
);

// Forbid GitHub writes unless the user carries the "github-writer" role tag.
forbid(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"GitHub___create_issue",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
)
unless {
  principal.hasTag("role") && principal.getTag("role") == "github-writer"
};

forbid(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"GitHub___update_issue",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
)
unless {
  principal.hasTag("role") && principal.getTag("role") == "github-writer"
};

forbid(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"GitHub___create_pull_request",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
)
unless {
  principal.hasTag("role") && principal.getTag("role") == "github-writer"
};
