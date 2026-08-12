// Policy 4: Block queries exceeding the row-limit threshold, and gate bulk export
// behind the "data-engineer" role.
// Resource ARN is substituted at deploy time (token __GATEWAY_ARN__).

// Forbid database queries requesting more than 1000 rows.
forbid(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"DatabaseAPI___execute_query",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
)
when {
  // maxRows is JSON type "number", which the policy-engine Cedar schema maps to
  // the `decimal` extension type. Comparing a decimal to a Long literal (`> 1000`)
  // is a type error; use the decimal extension's .greaterThan(decimal("...")).
  context.input has maxRows &&
  context.input.maxRows.greaterThan(decimal("1000.0"))
};

// Forbid bulk export tool unless user has "data-engineer" role.
forbid(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"DatabaseAPI___bulk_export",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
)
unless {
  principal.hasTag("role") &&
  principal.getTag("role") == "data-engineer"
};
