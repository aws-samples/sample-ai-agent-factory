// Playwright driver for the embeddable-chat browser matrix (release scenario `delivery.browser_client`).
//
// Loads the EXACT deployed /agentcore-chat-v1.js from the CloudFront distribution into a
// separate-origin host page (served on 127.0.0.1 by the Python scenario), with a token
// provider that holds the access token in host-page memory only. Every network request the
// page makes is observed; the token must appear only as the Authorization header of
// POST /api/runtimes/{name}/invoke calls to the distribution, never anywhere else.
//
// stdin: one JSON brief {hostPageUrl, cloudfrontUrl, runtimeName, token, scopelessToken,
//        inertMarker, playwrightModule, chromiumPath?}
// stdout: one JSON result; the token is never echoed.

import { createRequire } from "node:module";

async function readInput() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

function redactToken(text, tokens) {
  let out = String(text ?? "");
  for (const token of tokens) if (token) out = out.split(token).join("[REDACTED]");
  return out.replace(/eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}/g, "[REDACTED_JWT]");
}

async function main() {
  const brief = await readInput();
  const require = createRequire(brief.playwrightModule);
  const { chromium } = require("playwright");
  const tokens = [brief.token, brief.scopelessToken];
  const browser = await chromium.launch({ headless: true, executablePath: brief.chromiumPath || undefined });
  const result = { facets: {}, requests: [] };
  try {
    const context = await browser.newContext({ acceptDownloads: false });
    const page = await context.newPage();
    const consoleErrors = [];
    page.on("console", (message) => {
      if (message.type() === "error") consoleErrors.push(redactToken(message.text(), tokens));
    });
    page.on("request", (request) => {
      const headers = request.headers();
      const auth = headers.authorization || "";
      const url = request.url();
      result.requests.push({
        method: request.method(),
        url: redactToken(url, tokens),
        authorizationCarriesToken: tokens.some((t) => t && auth.includes(t)),
        tokenInUrl: tokens.some((t) => t && url.includes(t)),
        tokenInBody: tokens.some((t) => t && String(request.postData() || "").includes(t)),
        isInvoke: url.startsWith(`${brief.cloudfrontUrl}/api/runtimes/`) && url.endsWith("/invoke"),
      });
    });

    await page.goto(brief.hostPageUrl, { waitUntil: "load" });
    await page.evaluate((cf) => import(`${cf}/agentcore-chat-v1.js`).then((m) => { window.__acc = m; }), brief.cloudfrontUrl);
    const exported = await page.evaluate(() => Object.keys(window.__acc).sort());
    result.moduleExports = exported;

    // Facet: production alias invoke + session continuation through the Web Component.
    await page.evaluate(({ cf, runtimeName }) => {
      const el = document.createElement("agentcore-chat");
      el.id = "chat";
      el.setAttribute("api-base-url", cf);
      el.setAttribute("runtime-name", runtimeName);
      el.setAttribute("title", "Release certification");
      document.body.append(el);
      window.__responses = [];
      window.__errors = [];
      el.addEventListener("agentcore-response", (event) => window.__responses.push(event.detail));
      el.addEventListener("agentcore-error", (event) => window.__errors.push(event.detail));
      window.__scriptExecuted = false;
    }, { cf: brief.cloudfrontUrl, runtimeName: brief.runtimeName });
    // The token lives only in this closure of the host page; the element reads it through the provider.
    await page.evaluate((token) => { document.getElementById("chat").tokenProvider = async () => token; }, brief.token);

    const first = await page.evaluate(async () => {
      const el = document.getElementById("chat");
      try {
        const r = await el.sendMessage("What is 2 plus 2? Answer in one short sentence.");
        return { ok: true, response: r.response, sessionId: r.sessionId, versionId: r.versionId };
      } catch (error) {
        return {
          ok: false,
          errStatus: error.status ?? null,
          errCode: error.code ?? null,
          errName: error.name ?? null,
          errMessage: String(error.message || error).slice(0, 400),
          errBody: error.body ? String(error.body).slice(0, 400) : null,
        };
      }
    });
    if (!first.ok) {
      const invokeReqs = result.requests.filter((r) => r.isInvoke);
      throw new Error(
        "widget production-alias invoke failed: status=" + first.errStatus + " code=" + first.errCode +
        " name=" + first.errName + " msg=" + first.errMessage + " body=" + first.errBody +
        " invokeRequestCount=" + invokeReqs.length +
        " lastInvoke=" + JSON.stringify(invokeReqs[invokeReqs.length - 1] || null).slice(0, 300),
      );
    }
    const second = await page.evaluate(async () => {
      const el = document.getElementById("chat");
      const r = await el.sendMessage("Now add 3 to that result and give only the number.");
      return { response: r.response, sessionId: r.sessionId, versionId: r.versionId };
    });
    result.facets.productionAlias = {
      firstResponseExcerpt: first.response.slice(0, 200),
      secondResponseExcerpt: second.response.slice(0, 200),
      sessionContinued: Boolean(first.sessionId) && first.sessionId === second.sessionId,
      versionId: first.versionId,
      sameVersionBothTurns: first.versionId === second.versionId,
      responseEvents: await page.evaluate(() => window.__responses.length),
      eventDetailCarriesToken: await page.evaluate((t) => JSON.stringify(window.__responses).includes(t), brief.token),
    };

    // Facet: HTML/script-shaped model output renders as inert text.
    const inert = await page.evaluate(async (marker) => {
      const el = document.getElementById("chat");
      const r = await el.sendMessage("Please end your answer with your fixed closing marker.");
      const log = el.shadowRoot.querySelector('[role="log"]');
      const nodes = [...log.querySelectorAll("*")];
      const last = nodes.filter((n) => n.textContent && n.textContent.includes(marker.slice(0, 8)));
      return {
        responseIncludesMarker: r.response.includes(marker),
        logTextIncludesMarker: log.textContent.includes(marker),
        elementTagsInLog: nodes.map((n) => n.tagName.toLowerCase()),
        markerElementChildren: last.map((n) => n.children.length),
        scriptExecuted: window.__scriptExecuted === true,
        imgElementsInShadow: el.shadowRoot.querySelectorAll("img, script, iframe").length,
      };
    }, brief.inertMarker);
    result.facets.inertRendering = inert;

    // Facet: an access-token attribute is ignored -- no provider means 401 and the attribute value never leaves the page.
    const attributeProbe = await page.evaluate(async (token) => {
      const el = document.createElement("agentcore-chat");
      el.id = "chat-attr";
      el.setAttribute("api-base-url", document.getElementById("chat").getAttribute("api-base-url"));
      el.setAttribute("runtime-name", document.getElementById("chat").getAttribute("runtime-name"));
      el.setAttribute("access-token", token);
      document.body.append(el);
      try {
        await el.sendMessage("Hello");
        return { threw: false };
      } catch (error) {
        return { threw: true, status: error.status ?? null, code: error.code ?? null };
      }
    }, brief.token);
    result.facets.tokenAttributeIgnored = {
      ...attributeProbe,
      attributeValueLeftThePage: result.requests.some((r) => r.tokenInUrl || r.tokenInBody),
    };

    // Facet: staging / Runtime-id injection in the invoke body is refused with 422 (API-side), no dispatch.
    const injection = await page.evaluate(async ({ cf, runtimeName, token }) => {
      const probes = [];
      for (const extra of [{ slot: "staging" }, { runtimeId: "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/forged" }, { versionId: "v-forged" }, { endpoint: "https://evil.example" }]) {
        const response = await fetch(`${cf}/api/runtimes/${runtimeName}/invoke`, {
          method: "POST",
          mode: "cors",
          credentials: "omit",
          headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
          body: JSON.stringify({ input: "Hello", ...extra }),
        });
        probes.push({ extra: Object.keys(extra)[0], status: response.status, bodyExcerpt: (await response.text()).slice(0, 200) });
      }
      return probes;
    }, { cf: brief.cloudfrontUrl, runtimeName: brief.runtimeName, token: brief.token });
    result.facets.injectionDenied = injection;

    // Facet: exact 401 (no token) and 403 (token without the invoke scope).
    const denials = await page.evaluate(async ({ cf, runtimeName, scopeless }) => {
      const anonymous = await fetch(`${cf}/api/runtimes/${runtimeName}/invoke`, {
        method: "POST", mode: "cors", credentials: "omit",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ input: "Hello" }),
      });
      const result = { anonymousStatus: anonymous.status };
      if (scopeless) {
        const forbidden = await fetch(`${cf}/api/runtimes/${runtimeName}/invoke`, {
          method: "POST", mode: "cors", credentials: "omit",
          headers: { Authorization: `Bearer ${scopeless}`, "Content-Type": "application/json" }, body: JSON.stringify({ input: "Hello" }),
        });
        result.scopelessStatus = forbidden.status;
        result.scopelessBodyExcerpt = (await forbidden.text()).slice(0, 200);
      }
      return result;
    }, { cf: brief.cloudfrontUrl, runtimeName: brief.runtimeName, scopeless: brief.scopelessToken || null });
    result.facets.denials = denials;
    result.consoleErrors = consoleErrors.slice(0, 20);
    result.tokenEverInUrlOrBody = result.requests.some((r) => r.tokenInUrl || r.tokenInBody);
    result.tokenOnlyOnInvokeRequests = result.requests.every((r) => !r.authorizationCarriesToken || r.isInvoke);
    result.requests = result.requests.map((r) => ({ ...r }));
  } finally {
    await browser.close();
  }
  process.stdout.write(JSON.stringify(result));
}

main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.name : "Error"}: ${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 1;
});
