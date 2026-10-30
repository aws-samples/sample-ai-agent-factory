// Playwright driver for `deployed-cloudfront-ui-authenticated-cfn-python-downloads-verified`
// (release control `delivery.downloadable_assets`, evidence kind `ui_cloudfront`).
//
// Drives the EXACT deployed CloudFront UI the way a customer does: signs in through the
// shipped Amplify Authenticator form (the token is minted by the UI itself, never injected),
// seeds the canvas through the product's own template gallery, opens the deploy panel and
// clicks the two download controls. It saves the bytes the browser received and reads the
// receipt digests the UI renders after its own in-browser SHA-256 verification.
//
// stdin: one JSON brief {cloudfrontUrl, username, password, downloadDir, playwrightModule,
//        chromiumPath?, templateName?, enableTimeoutMs?}
// stdout: one JSON result. The password is never echoed; URLs are recorded without query
//         strings, request bodies and headers are never recorded, and JWT-shaped strings are
//         redacted from every text excerpt.

import { createRequire } from "node:module";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import path from "node:path";

const HEX64 = /[0-9a-f]{64}/;

async function readInput() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

function redact(text, secrets) {
  let out = String(text ?? "");
  for (const secret of secrets) if (secret) out = out.split(secret).join("[REDACTED]");
  return out.replace(/eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}/g, "[REDACTED_JWT]");
}

function requireText(brief, name) {
  const value = brief[name];
  if (typeof value !== "string" || !value.trim()) throw new Error(`${name} must be a non-empty string`);
  return value;
}

function apiPath(url, cloudfrontUrl) {
  if (!url.startsWith(`${cloudfrontUrl}/api/`)) return null;
  return url.slice(cloudfrontUrl.length).split("?")[0];
}

async function sha256File(file) {
  return createHash("sha256").update(await readFile(file)).digest("hex");
}

async function panelText(page) {
  const panel = page.locator('[aria-labelledby="deploy-panel-title"]').first();
  return (await panel.count()) ? (await panel.innerText()).slice(0, 4000) : "";
}

function digestAfter(text, label) {
  const index = text.indexOf(label);
  if (index < 0) return null;
  const match = HEX64.exec(text.slice(index + label.length, index + label.length + 400));
  return match ? match[0] : null;
}

async function waitEnabled(locator, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if ((await locator.count()) && (await locator.first().isEnabled())) return true;
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  return false;
}

async function captureDownload(page, button, downloadDir, label, secrets) {
  const [download] = await Promise.all([page.waitForEvent("download", { timeout: 180_000 }), button.first().click()]);
  const filename = download.suggestedFilename();
  if (!filename || /[\\/]/.test(filename) || filename === "." || filename === "..") {
    throw new Error(`${label}: the browser proposed an unusable filename`);
  }
  const target = path.join(downloadDir, filename);
  await download.saveAs(target);
  const failure = await download.failure();
  if (failure) throw new Error(`${label}: download failed: ${redact(failure, secrets)}`);
  const sha256 = await sha256File(target);
  // The UI renders the receipt only after its own SubtleCrypto verification succeeded.
  const deadline = Date.now() + 60_000;
  let archiveReceipt = null;
  let manifestReceipt = null;
  let text = "";
  while (Date.now() < deadline && (!archiveReceipt || !manifestReceipt)) {
    text = await panelText(page);
    archiveReceipt = archiveReceipt || digestAfter(text, "archive SHA-256");
    manifestReceipt = manifestReceipt || digestAfter(text, "EXPECTED_ARTIFACT_MANIFEST_SHA256=");
    if (!archiveReceipt || !manifestReceipt) await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  return {
    filename,
    savedPath: target,
    bytes: (await readFile(target)).length,
    sha256,
    receiptArchiveSha256: archiveReceipt,
    receiptManifestSha256: manifestReceipt,
    receiptMatchesBytes: archiveReceipt === sha256,
  };
}

async function main() {
  const brief = await readInput();
  const cloudfrontUrl = requireText(brief, "cloudfrontUrl").replace(/\/+$/, "");
  const username = requireText(brief, "username");
  const password = requireText(brief, "password");
  const downloadDir = requireText(brief, "downloadDir");
  const enableTimeoutMs = Number(brief.enableTimeoutMs) > 0 ? Number(brief.enableTimeoutMs) : 180_000;
  const secrets = [password];
  const require = createRequire(requireText(brief, "playwrightModule"));
  const { chromium } = require("playwright");

  const result = {
    cloudfrontUrl,
    signedIn: false,
    templateUsed: null,
    apiCalls: [],
    consoleErrors: [],
    blockedReasons: {},
    cfn: null,
    python: null,
  };
  const browser = await chromium.launch({ headless: true, executablePath: brief.chromiumPath || undefined });
  try {
    const context = await browser.newContext({ acceptDownloads: true });
    const page = await context.newPage();
    page.on("console", (message) => {
      if (message.type() === "error") result.consoleErrors.push(redact(message.text().slice(0, 300), secrets));
    });
    page.on("response", (response) => {
      const route = apiPath(response.url(), cloudfrontUrl);
      if (route) result.apiCalls.push({ method: response.request().method(), path: route, status: response.status() });
    });

    // 1. The shipped sign-in form mints the session; nothing is injected into storage.
    await page.goto(`${cloudfrontUrl}/`, { waitUntil: "load" });
    const form = page.locator("[data-amplify-authenticator]");
    await form.first().waitFor({ state: "visible", timeout: 60_000 });
    await form.locator('input[name="username"]').first().fill(username);
    await form.locator('input[name="password"]').first().fill(password);
    await form.locator('button[type="submit"]').first().click();
    await form.first().waitFor({ state: "detached", timeout: 90_000 });
    result.signedIn = true;

    // 2. Seed the canvas through the product's own gallery path (no drag-and-drop).
    const browse = page.getByRole("button", { name: "Browse templates" });
    if (!(await waitEnabled(browse, 60_000))) throw new Error("the Browse templates control never enabled");
    await browse.first().click();
    const gallery = page.locator('[aria-labelledby="template-gallery-title"]').first();
    await gallery.waitFor({ state: "visible", timeout: 30_000 });
    let card = gallery.getByRole("button", { name: "Use Template" });
    if (brief.templateName) {
      // Scope to the specific template CARD (div.rounded-lg — the same anchor the heading xpath
      // below uses), not any ancestor grid div that also contains the name's text, which would
      // make card.first() land on the first gallery card instead of the requested one.
      const scoped = gallery
        .locator("div.rounded-lg")
        .filter({ hasText: String(brief.templateName) })
        .getByRole("button", { name: "Use Template" });
      if (await scoped.count()) card = scoped;
    }
    if (!(await card.count())) {
      result.blockedReasons.gallery = redact((await gallery.innerText()).slice(0, 600), secrets);
      throw new Error("no template is usable in this Region (no 'Use Template' control)");
    }
    const heading = card.first().locator("xpath=ancestor::div[contains(@class,'rounded-lg')][1]//h3");
    result.templateUsed = (await heading.count()) ? (await heading.first().innerText()).trim() : "(first available)";
    await card.first().click();
    await gallery.waitFor({ state: "detached", timeout: 30_000 });

    // 3. The shipped Deploy control opens the panel.
    const deploy = page.getByTestId("deploy-button");
    if (!(await waitEnabled(deploy, enableTimeoutMs))) throw new Error("the Deploy control never enabled for the seeded canvas");
    await deploy.first().click();
    await page.locator('[aria-labelledby="deploy-panel-title"]').first().waitFor({ state: "visible", timeout: 30_000 });

    // 3b. Supply every required resource tag. Required tags render a "*" marker
    // (span.text-amber-700) inside their <label>, with the value <input> nested in the same label;
    // filling any non-empty value resolves missingRequired (presence, not a specific value). Capture
    // diagnostics of all labeled inputs so a miss explains itself in the thrown error.
    const tagValue = String(brief.tagValue || "acf-release-cert");
    // Resource-tag inputs render only AFTER the async tag-governance fetch (/api/settings/tags)
    // resolves, which happens after the panel opens — filling immediately finds none. Wait for the
    // tag-policy labels (their key contains ':', e.g. platform:application) to render, then fill each.
    let filledTags = 0;
    const tagLabels = page.locator("label").filter({ hasText: /[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+/ });
    try {
      await tagLabels.first().waitFor({ state: "visible", timeout: 60_000 });
    } catch {
      /* no org tag policies configured in this environment; leave filledTags = 0 */
    }
    const tagCount = await tagLabels.count();
    for (let i = 0; i < tagCount; i += 1) {
      const inp = tagLabels.nth(i).locator('input[type="text"]');
      if (await inp.count()) {
        await inp.first().fill(tagValue);
        filledTags += 1;
      }
    }
    result.requiredTagsFilled = filledTags;
    result.tagDiagnostics = [];
    const labeledInputs = page.locator("label:has(input)");
    const li = await labeledInputs.count();
    for (let i = 0; i < Math.min(li, 30); i += 1) {
      const inp = labeledInputs.nth(i).locator("input").first();
      result.tagDiagnostics.push({
        lab: (await labeledInputs.nth(i).innerText().catch(() => "")).replace(/\s+/g, " ").slice(0, 50),
        ph: await inp.getAttribute("placeholder").catch(() => null),
        val: await inp.inputValue().catch(() => ""),
      });
    }

    // 4. Both download controls, each gated by the panel's own governance checks.
    const cfnButton = page.getByRole("button", { name: /Download CloudFormation Template/ });
    if (!(await waitEnabled(cfnButton, enableTimeoutMs))) {
      const cfnTitle = (await cfnButton.first().getAttribute("title").catch(() => "")) || "";
      result.blockedReasons.cfn = redact(cfnTitle, secrets).slice(0, 400);
      throw new Error(
        "the CloudFormation download control never enabled: TITLE=[" + redact(cfnTitle, secrets) +
        "] filled=" + result.requiredTagsFilled + " diag=" + JSON.stringify(result.tagDiagnostics).slice(0, 700),
      );
    }
    result.cfn = await captureDownload(page, cfnButton, downloadDir, "cfn", secrets);

    const pythonButton = page.getByRole("button", { name: /Export as Python/ });
    if (!(await waitEnabled(pythonButton, enableTimeoutMs))) {
      const reason = page.locator("#python-export-blocked-reason");
      result.blockedReasons.python = redact(
        (await reason.count()) ? await reason.first().innerText() : await panelText(page),
        secrets,
      ).slice(0, 1200);
      throw new Error("the Python export control never enabled");
    }
    result.python = await captureDownload(page, pythonButton, downloadDir, "python", secrets);
    result.consoleErrors = result.consoleErrors.slice(0, 20);
  } finally {
    await browser.close();
  }
  process.stdout.write(redact(JSON.stringify(result), secrets));
}

main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.name : "Error"}: ${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 1;
});
