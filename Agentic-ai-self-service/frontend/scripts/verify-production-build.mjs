#!/usr/bin/env node

import { readFile, readdir, stat } from 'node:fs/promises';
import { Buffer } from 'node:buffer';
import { basename, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const DIRECT_API_GATEWAY_URL =
  /https:\/\/[a-z0-9-]+\.execute-api\.[a-z0-9-]+\.amazonaws\.com(?:\/[^"'`\s]*)?/gi;

function requireValue(name, value) {
  if (!value?.trim()) {
    throw new Error(`${name} must be set when verifying a production build.`);
  }
  return value.trim();
}

function validateApiBaseUrl(value) {
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error('VITE_API_BASE_URL must be an absolute HTTPS URL.');
  }

  if (parsed.protocol !== 'https:') {
    throw new Error('VITE_API_BASE_URL must use HTTPS.');
  }
  if (value !== parsed.origin) {
    throw new Error(
      'VITE_API_BASE_URL must be a canonical origin without a trailing slash, path, query, or fragment.',
    );
  }
  return parsed.origin;
}

async function walkJavaScriptFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const path = resolve(directory, entry.name);
    if (entry.isDirectory()) {
      files.push(...await walkJavaScriptFiles(path));
    } else if (entry.isFile() && entry.name.endsWith('.js')) {
      files.push(path);
    }
  }
  return files;
}

function entryScriptFromHtml(html) {
  const scripts = [...html.matchAll(/<script\b[^>]*\bsrc=["']([^"']+)["'][^>]*>/gi)]
    .map((match) => match[1])
    .filter((source) => source.endsWith('.js'));
  if (scripts.length !== 1) {
    throw new Error(`Expected exactly one JavaScript entry script in index.html; found ${scripts.length}.`);
  }
  return scripts[0];
}

function buildConfigFromHtml(html) {
  const tag = html.match(
    /<meta\b(?=[^>]*\bname=["']agentcore-build-config["'])[^>]*>/i,
  )?.[0];
  if (!tag) {
    throw new Error('index.html is missing the agentcore-build-config manifest.');
  }
  const encoded = tag.match(/\bcontent=["']([^"']+)["']/i)?.[1];
  if (!encoded) {
    throw new Error('The agentcore-build-config manifest has no content.');
  }

  let decoded;
  try {
    decoded = JSON.parse(Buffer.from(encoded, 'base64url').toString('utf8'));
  } catch {
    throw new Error('The agentcore-build-config manifest is not valid base64url JSON.');
  }
  if (!decoded || typeof decoded !== 'object' || Array.isArray(decoded)) {
    throw new Error('The agentcore-build-config manifest must decode to an object.');
  }
  return decoded;
}

function pathInside(directory, browserPath) {
  const path = resolve(directory, browserPath.replace(/^\/+/, ''));
  const root = `${resolve(directory)}/`;
  if (!path.startsWith(root)) {
    throw new Error(`Entry script resolves outside the build directory: ${browserPath}`);
  }
  return path;
}

export async function verifyProductionBuild({
  distDir = 'dist',
  apiBaseUrl,
  awsRegion,
  userPoolId,
  userPoolClientId,
}) {
  const expectedApiOrigin = validateApiBaseUrl(requireValue('VITE_API_BASE_URL', apiBaseUrl));
  const expectedAwsRegion = requireValue('VITE_AWS_REGION', awsRegion);
  const expectedUserPoolId = requireValue('VITE_COGNITO_USER_POOL_ID', userPoolId);
  const expectedUserPoolClientId = requireValue('VITE_COGNITO_CLIENT_ID', userPoolClientId);
  const resolvedDist = resolve(distDir);

  const indexPath = resolve(resolvedDist, 'index.html');
  const indexHtml = await readFile(indexPath, 'utf8');
  const buildConfig = buildConfigFromHtml(indexHtml);
  const expectedBuildConfig = {
    apiBaseUrl: expectedApiOrigin,
    awsRegion: expectedAwsRegion,
    userPoolId: expectedUserPoolId,
    userPoolClientId: expectedUserPoolClientId,
  };
  for (const [name, expected] of Object.entries(expectedBuildConfig)) {
    if (buildConfig[name] !== expected) {
      throw new Error(
        `Build manifest ${name} does not match the deployment input: `
          + `${JSON.stringify(buildConfig[name] ?? null)} != ${JSON.stringify(expected)}.`,
      );
    }
  }

  const entryBrowserPath = entryScriptFromHtml(indexHtml);
  const entryPath = pathInside(resolvedDist, entryBrowserPath);
  const entryStats = await stat(entryPath);
  if (!entryStats.isFile()) {
    throw new Error(`Entry script is not a file: ${entryBrowserPath}`);
  }

  const javascriptFiles = await walkJavaScriptFiles(resolvedDist);
  const javascript = await Promise.all(
    javascriptFiles.map(async (path) => ({ path, source: await readFile(path, 'utf8') })),
  );
  const allJavaScript = javascript.map(({ source }) => source).join('\n');
  const entrySource = await readFile(entryPath, 'utf8');

  for (const [name, expected] of [
    ['VITE_API_BASE_URL', expectedApiOrigin],
    ['VITE_AWS_REGION', expectedAwsRegion],
    ['VITE_COGNITO_USER_POOL_ID', expectedUserPoolId],
    ['VITE_COGNITO_CLIENT_ID', expectedUserPoolClientId],
  ]) {
    if (!allJavaScript.includes(expected)) {
      throw new Error(`${name} is absent from the generated JavaScript.`);
    }
  }

  const directApiUrls = [...new Set(allJavaScript.match(DIRECT_API_GATEWAY_URL) ?? [])];
  if (directApiUrls.length > 0) {
    throw new Error(
      `Generated JavaScript embeds a direct API Gateway URL instead of the same-origin frontend: ${directApiUrls.join(', ')}`,
    );
  }

  const authChunks = javascriptFiles.filter((path) => /^AuthWrapper-[A-Za-z0-9_-]+\.js$/.test(basename(path)));
  if (authChunks.length !== 1) {
    throw new Error(`Expected exactly one AuthWrapper chunk; found ${authChunks.length}.`);
  }
  const authChunk = basename(authChunks[0]);
  if (!entrySource.includes(authChunk)) {
    throw new Error(`The entry script does not reference its AuthWrapper chunk: ${authChunk}`);
  }

  const awsVendorChunks = javascriptFiles.filter((path) => /^aws-vendor-[A-Za-z0-9_-]+\.js$/.test(basename(path)));
  if (awsVendorChunks.length !== 1) {
    throw new Error(`Expected exactly one AWS vendor chunk; found ${awsVendorChunks.length}.`);
  }
  const awsVendorChunk = basename(awsVendorChunks[0]);
  if (!entrySource.includes(awsVendorChunk)) {
    throw new Error(`The entry script does not reference its AWS vendor chunk: ${awsVendorChunk}`);
  }

  return {
    distDir: resolvedDist,
    entryScript: basename(entryPath),
    authChunk,
    awsVendorChunk,
    javascriptFiles: javascriptFiles.length,
    apiOrigin: expectedApiOrigin,
    awsRegion: expectedAwsRegion,
    buildConfig,
  };
}

async function main() {
  const result = await verifyProductionBuild({
    distDir: process.argv[2] || 'dist',
    apiBaseUrl: process.env.VITE_API_BASE_URL,
    awsRegion: process.env.VITE_AWS_REGION,
    userPoolId: process.env.VITE_COGNITO_USER_POOL_ID,
    userPoolClientId: process.env.VITE_COGNITO_CLIENT_ID,
  });
  console.log(
    `Production frontend verified: ${result.entryScript}, ${result.authChunk}, `
      + `${result.awsVendorChunk}, API ${result.apiOrigin}, region ${result.awsRegion}`,
  );
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(`Production frontend verification failed: ${error.message}`);
    process.exitCode = 1;
  });
}
