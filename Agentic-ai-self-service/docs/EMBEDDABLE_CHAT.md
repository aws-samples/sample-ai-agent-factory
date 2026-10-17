# Embeddable Chat and Browser Client v1

[← Back to README](../README.md)

`agentcore-chat-v1.js` is a dependency-free ES module copied into every frontend
build and served from the deployed CloudFront distribution. It exports:

- `AgentCoreClient` — a typed-shape browser client for one production agent.
- `AgentCoreClientError` — carries `status`, `code`, and `retryAfter`.
- `<agentcore-chat>` / `AgentCoreChatElement` — an accessible Shadow DOM chat UI.
- `defineAgentCoreChat()` — registers the default or a caller-selected tag name.

The module calls only `POST /api/runtimes/{runtime_name}/invoke`. That stable alias
always delegates to the owner-checked **production** slot and accepts no account,
role, Region, Runtime ARN/id, endpoint, version, staging slot, simulation, or warmup
input. It inherits the platform's atomic invocation authorization, exact-version
resolution, OBO handoff, Guardrails, Cedar, audit, and response validation.

## Host integration

Load the module from the same CloudFront distribution that hosts the platform:

```html
<agentcore-chat
  id="support-chat"
  api-base-url="https://YOUR_DISTRIBUTION.cloudfront.net"
  runtime-name="customer_support"
  title="Customer support"
></agentcore-chat>

<script type="module">
  import 'https://YOUR_DISTRIBUTION.cloudfront.net/agentcore-chat-v1.js';
  import { fetchAuthSession } from '/assets/host-auth.js';

  const chat = document.querySelector('#support-chat');
  chat.tokenProvider = async () => {
    const session = await fetchAuthSession();
    return session.tokens.accessToken.toString();
  };
</script>
```

`/assets/host-auth.js` represents the embedding application's existing bundled
identity adapter. The only required integration is a function returning a current
Cognito access JWT.
The widget deliberately has **no** `access-token` attribute and never reads cookies,
localStorage, or sessionStorage. Keep tokens in host application memory and refresh
through the IdP before expiry.

Programmatic use:

```js
import {
  AgentCoreClient,
  AgentCoreClientError,
} from 'https://YOUR_DISTRIBUTION.cloudfront.net/agentcore-chat-v1.js';

const client = new AgentCoreClient({
  baseUrl: 'https://YOUR_DISTRIBUTION.cloudfront.net',
  tokenProvider: getCurrentAccessToken,
});

try {
  const result = await client.invoke({
    runtimeName: 'customer_support',
    input: 'Where is order 123?',
    sessionId: currentSessionId,
  });
  currentSessionId = result.sessionId;
} catch (error) {
  if (error instanceof AgentCoreClientError && error.status === 429) {
    scheduleRetryAfter(error.retryAfter);
  }
  throw error;
}
```

Type declarations ship at `agentcore-chat-v1.d.ts`. Copy both files into an npm
package or configure your TypeScript resolver to associate the declaration with the
module URL when a remote-import type plugin is available.

## Events and rendering

The component dispatches composed, bubbling `agentcore-response` and
`agentcore-error` events. Event detail never includes the access token. Model output
is assigned with `textContent`, not interpreted as HTML; HTML/script-shaped model
content therefore renders as inert text. One request may be in flight at a time.
`newConversation()` clears in-memory messages/session state.

The client validates input/history lengths and Runtime names before requesting a
token. It uses `credentials: "omit"`, `cache: "no-store"`, and `redirect: "error"`.
It performs **no automatic POST retry** because a timeout can hide a completed agent
or tool side effect. Callers decide whether a typed failure is safe to retry.
Responses over 2 MiB or malformed JSON fail closed.

## CORS and host CSP

CloudFront returns non-credentialed CORS headers (`Allow-Origin: *`, no
`Allow-Credentials`) and explicitly allows `Authorization` and `Content-Type`.
`Retry-After` is exposed. The bearer remains the authorization boundary; no ambient
browser credential is accepted.

The embedding site's Content Security Policy must allow the distribution under
`script-src` and `connect-src`. The platform's own `frame-ancestors 'none'` and
`X-Frame-Options: DENY` remain unchanged because this is a Web Component, not an
iframe.

## Verification and cleanup

Offline gates:

```bash
cd frontend
./node_modules/.bin/vitest --run --maxWorkers=1 \
  src/embed/agentcore-chat-v1.test.js \
  scripts/verify-production-build.test.mjs
npm run lint
npm run build
npm run verify:production-build
```

Live release evidence must load the exact deployed
`/agentcore-chat-v1.js` from CloudFront in a separate-origin page, authenticate a
real user, invoke the production alias, continue a session, render HTML-shaped output
as text, and capture exact 401/403/429 behavior. The release matrix must also prove a
staging/Runtime-id injection receives `422` and no Runtime dispatch.

The widget creates no resource of its own. Stack teardown removes its S3 object and
CloudFront distribution under the existing frontend lifecycle; residual inventory
must confirm the versioned JS and declaration are no longer reachable.
