// Policy 3: Forbid destructive database operations for everyone, and constrain
// execute_query to read-only SELECT statements.
// Resource ARN is substituted at deploy time (token __GATEWAY_ARN__).

// Block ALL users from calling destructive database tools regardless of role.
// One single-action forbid per tool: the policy engine does not match Cedar
// action set-membership (`action in [...]`) for tool authorization.
forbid(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"DatabaseAPI___drop_table",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
);

forbid(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"DatabaseAPI___truncate_table",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
);

forbid(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"DatabaseAPI___delete_records",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
);

// Allow read-only database queries for all authenticated users.
permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"DatabaseAPI___execute_query",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
)
when {
  context.input has query &&
  context.input.query like "SELECT *"
};

// Forbid execute_query if it contains dangerous patterns.
forbid(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"DatabaseAPI___execute_query",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
)
when {
  context.input has query &&
  (context.input.query like "*DROP*" ||
   context.input.query like "*DELETE*" ||
   context.input.query like "*TRUNCATE*" ||
   context.input.query like "*INSERT*" ||
   context.input.query like "*UPDATE*")
};
