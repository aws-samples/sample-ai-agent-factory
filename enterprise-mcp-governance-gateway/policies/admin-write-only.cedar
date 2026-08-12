// Policy 2: Only users with role "admin" can create/update/delete docs.
// Resource ARN is substituted at deploy time (token __GATEWAY_ARN__).
// NOTE: One single-action permit per tool. The policy engine does not match
// Cedar action set-membership (`action in [...]`) for tool authorization.
//
// IMPORTANT: these permits depend on a `role` principal tag. With this Cognito
// setup `custom:role` is present only in the ID token, NOT the access token the
// gateway validates — so for the demo users these permits do not fire and writes
// stay DENIED (the safe default). To demonstrate role-based writes, surface the
// role in the ACCESS token (Cognito pre-token-generation V2 Lambda, or a custom
// OAuth scope per role) and these policies enforce as written.
permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"DocsAPI___create_page",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
)
when {
  principal.hasTag("role") &&
  principal.getTag("role") == "admin"
};

permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"DocsAPI___update_page",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
)
when {
  principal.hasTag("role") &&
  principal.getTag("role") == "admin"
};

permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"DocsAPI___delete_page",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
)
when {
  principal.hasTag("role") &&
  principal.getTag("role") == "admin"
};
