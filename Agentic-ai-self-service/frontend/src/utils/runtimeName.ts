/**
 * Which runtime name the management tabs (versions, evals, cost, observability,
 * triggers) must query AgentCore with.
 *
 * The deploy normalizes the canvas name to the AgentCore regex (e.g. "My Agent"
 * -> "My_Agent") and the backend serves that as `runtime_name`. Querying the raw
 * canvas name returned 400 and left every tab empty. So prefer the deployed
 * name, then a restored deployment's name, and fall back to the canvas name only
 * before anything has been deployed (so the tabs' own "deploy first" empty state
 * still shows a sensible value).
 */
export function resolveManagementRuntimeName(
  deployedRuntimeName: string | null | undefined,
  restoredRuntimeName: string | null | undefined,
  canvasName: string | null | undefined,
): string | null {
  return deployedRuntimeName ?? restoredRuntimeName ?? canvasName ?? null;
}
