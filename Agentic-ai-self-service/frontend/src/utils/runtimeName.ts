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
const AGENTCORE_RUNTIME_NAME = /^[A-Za-z][A-Za-z0-9_]*$/;

export function resolveManagementRuntimeName(
  deployedRuntimeName: string | null | undefined,
  restoredRuntimeName: string | null | undefined,
  canvasName: string | null | undefined,
): string | null {
  const candidate = deployedRuntimeName ?? restoredRuntimeName ?? canvasName ?? null;
  // Defense in depth: the management endpoints validate the name with this exact
  // pattern and return 400 otherwise, and a browser silently normalizes "." / ".."
  // path segments. Only hand a name to the tabs when it already matches, so a
  // pre-deploy canvas name with spaces — or any hostile value — is never placed
  // in a request path; the tabs show their "deploy first" empty state instead.
  if (!candidate || !AGENTCORE_RUNTIME_NAME.test(candidate)) return null;
  return candidate;
}
