// Policy 1: Allow all authenticated OAuth users to call read-only Docs tools.
// Resource ARN is substituted at deploy time (token __GATEWAY_ARN__).
// NOTE: The AgentCore policy engine does NOT match Cedar action set-membership
// (`action in [...]`) for tool authorization. Each tool MUST be its own
// single-action `action == AgentCore::Action::"..."` permit statement.
permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"DocsAPI___get_page",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
);

permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"DocsAPI___search_pages",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
);

permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"DocsAPI___list_spaces",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
);
