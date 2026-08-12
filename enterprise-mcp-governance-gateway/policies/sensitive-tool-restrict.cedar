// Policy 5: Restrict sensitive tools to specific users / scopes.
// Resource ARN is substituted at deploy time (token __GATEWAY_ARN__).

// Restrict the audit_logs tool to a specific user (PRODUCTION PATTERN / illustration).
// NOTE: with this Cognito setup the access-token `username` claim is the user's
// sub-UUID, not the email — so this exact match won't fire for the demo users
// (audit_logs stays denied, which is the safe default). In production, drive this
// from a stable identity claim present in your access token (e.g. a custom scope
// or a verified principal id) rather than an email.
permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"DatabaseAPI___query_audit_logs",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
)
when {
  principal.hasTag("username") &&
  principal.getTag("username") == "security-admin@example.com"
};

// PII export is restricted by OAuth scope.
//
// IMPORTANT (verified against this Cognito setup): the GATEWAY validates the
// Cognito ACCESS token, whose claims become Cedar principal tags. The access
// token reliably carries `sub`, `username`, and `scope` — but NOT `email` or
// `custom:role` (those live only in the ID token, which the gateway rejects).
// So identity policies MUST key on a claim present in the ACCESS token.
//
//   (a) PRODUCTION pattern: a dedicated OAuth scope (e.g. "mcp-gateway/pii:read")
//       granted only to privileged app clients/users via a Cognito resource
//       server. Replace the match below with that scope.
//   (b) DEMO: the demo users share Cognito's default `aws.cognito.signin.user.admin`
//       scope, so the match below lets an authenticated demo user exercise the
//       RESPONSE interceptor's PII redaction from a CLEAN deploy. This is NOT
//       fine-grained authz — it exists to make the redaction control demonstrable.
permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"DatabaseAPI___export_pii_report",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
)
when {
  principal.hasTag("scope") &&
  principal.getTag("scope") like "*aws.cognito.signin.user.admin*"
};
