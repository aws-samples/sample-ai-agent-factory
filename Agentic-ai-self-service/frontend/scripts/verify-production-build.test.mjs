import { afterEach, describe, expect, it } from 'vitest';
import { Buffer } from 'node:buffer';
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { verifyProductionBuild } from './verify-production-build.mjs';

const API_ORIGIN = 'https://frontend.example.com';
const AWS_REGION = 'eu-west-1';
const USER_POOL_ID = 'us-east-1_example';
const USER_POOL_CLIENT_ID = 'example-client-id';
const temporaryDirectories = [];

async function fixture({
  entrySource,
  authSource = 'export const AuthWrapper = true;',
  awsVendorSource = 'export const Amplify = true;',
  buildConfig = {
    apiBaseUrl: API_ORIGIN,
    awsRegion: AWS_REGION,
    userPoolId: USER_POOL_ID,
    userPoolClientId: USER_POOL_CLIENT_ID,
  },
} = {}) {
  const directory = await mkdtemp(join(tmpdir(), 'frontend-build-contract-'));
  temporaryDirectories.push(directory);
  const assets = join(directory, 'assets');
  await mkdir(assets);

  const authChunk = 'AuthWrapper-auth123.js';
  const awsVendorChunk = 'aws-vendor-aws123.js';
  const source = entrySource ?? [
    `const api = ${JSON.stringify(API_ORIGIN)};`,
    `const region = ${JSON.stringify(AWS_REGION)};`,
    `const pool = ${JSON.stringify(USER_POOL_ID)};`,
    `const client = ${JSON.stringify(USER_POOL_CLIENT_ID)};`,
    `const chunks = ["assets/${authChunk}", "assets/${awsVendorChunk}"];`,
  ].join('\n');

  const buildConfigMeta = buildConfig === null
    ? ''
    : `<meta name="agentcore-build-config" content="${Buffer.from(JSON.stringify(buildConfig)).toString('base64url')}">`;
  await writeFile(
    join(directory, 'index.html'),
    `${buildConfigMeta}<div id="root"></div><script type="module" src="/assets/index-app123.js"></script>`,
  );
  await writeFile(join(assets, 'index-app123.js'), source);
  if (authSource !== null) await writeFile(join(assets, authChunk), authSource);
  if (awsVendorSource !== null) await writeFile(join(assets, awsVendorChunk), awsVendorSource);
  return directory;
}

function options(distDir) {
  return {
    distDir,
    apiBaseUrl: API_ORIGIN,
    awsRegion: AWS_REGION,
    userPoolId: USER_POOL_ID,
    userPoolClientId: USER_POOL_CLIENT_ID,
  };
}

afterEach(async () => {
  await Promise.all(temporaryDirectories.splice(0).map((directory) => rm(directory, {
    recursive: true,
    force: true,
  })));
});

describe('production frontend build contract', () => {
  it('accepts an authenticated same-origin build', async () => {
    const distDir = await fixture();

    await expect(verifyProductionBuild(options(distDir))).resolves.toMatchObject({
      entryScript: 'index-app123.js',
      authChunk: 'AuthWrapper-auth123.js',
      awsVendorChunk: 'aws-vendor-aws123.js',
      apiOrigin: API_ORIGIN,
      awsRegion: AWS_REGION,
    });
  });

  it('rejects the unauthenticated local-build shape', async () => {
    const distDir = await fixture({ authSource: null });

    await expect(verifyProductionBuild(options(distDir))).rejects.toThrow(
      'Expected exactly one AuthWrapper chunk; found 0.',
    );
  });

  it('rejects a build whose entry does not load the auth chunk', async () => {
    const distDir = await fixture({
      entrySource: [
        `const api = ${JSON.stringify(API_ORIGIN)};`,
        `const region = ${JSON.stringify(AWS_REGION)};`,
        `const pool = ${JSON.stringify(USER_POOL_ID)};`,
        `const client = ${JSON.stringify(USER_POOL_CLIENT_ID)};`,
        'const chunks = ["assets/aws-vendor-aws123.js"];',
      ].join('\n'),
    });

    await expect(verifyProductionBuild(options(distDir))).rejects.toThrow(
      'The entry script does not reference its AuthWrapper chunk',
    );
  });

  it('rejects a stale or incorrectly configured API origin', async () => {
    const distDir = await fixture();

    await expect(verifyProductionBuild({
      ...options(distDir),
      apiBaseUrl: 'https://different.example.com',
    })).rejects.toThrow('Build manifest apiBaseUrl does not match the deployment input');
  });

  it('rejects direct API Gateway URLs even when the expected origin is present', async () => {
    const distDir = await fixture({
      entrySource: [
        `const api = ${JSON.stringify(API_ORIGIN)};`,
        `const region = ${JSON.stringify(AWS_REGION)};`,
        `const pool = ${JSON.stringify(USER_POOL_ID)};`,
        `const client = ${JSON.stringify(USER_POOL_CLIENT_ID)};`,
        'const stale = "https://abc123.execute-api.us-east-1.amazonaws.com";',
        'const chunks = ["assets/AuthWrapper-auth123.js", "assets/aws-vendor-aws123.js"];',
      ].join('\n'),
    });

    await expect(verifyProductionBuild(options(distDir))).rejects.toThrow(
      'Generated JavaScript embeds a direct API Gateway URL',
    );
  });

  it('requires the production authentication inputs', async () => {
    const distDir = await fixture();

    await expect(verifyProductionBuild({
      ...options(distDir),
      userPoolId: '',
    })).rejects.toThrow('VITE_COGNITO_USER_POOL_ID must be set');
  });

  it('rejects an API origin with a trailing slash before inspecting the artifact', async () => {
    const distDir = await fixture();

    await expect(verifyProductionBuild({
      ...options(distDir),
      apiBaseUrl: `${API_ORIGIN}/`,
    })).rejects.toThrow('must be a canonical origin without a trailing slash');
  });

  it('requires the production region input', async () => {
    const distDir = await fixture();

    await expect(verifyProductionBuild({
      ...options(distDir),
      awsRegion: '',
    })).rejects.toThrow('VITE_AWS_REGION must be set');
  });

  it('rejects a build without a self-describing configuration manifest', async () => {
    const distDir = await fixture({ buildConfig: null });

    await expect(verifyProductionBuild(options(distDir))).rejects.toThrow(
      'index.html is missing the agentcore-build-config manifest.',
    );
  });

  it('rejects a region value that only appears incidentally elsewhere', async () => {
    const distDir = await fixture({
      buildConfig: {
        apiBaseUrl: API_ORIGIN,
        awsRegion: '',
        userPoolId: `us-east-1_${AWS_REGION}`,
        userPoolClientId: USER_POOL_CLIENT_ID,
      },
    });

    await expect(verifyProductionBuild(options(distDir))).rejects.toThrow(
      'Build manifest awsRegion does not match the deployment input',
    );
  });
});
