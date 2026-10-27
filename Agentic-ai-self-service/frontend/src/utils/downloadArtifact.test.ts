/// <reference types="node" />
import { webcrypto } from 'node:crypto';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ARTIFACT_INTEGRITY_ERROR, downloadArtifact } from './downloadArtifact';

const ARCHIVE = new TextEncoder().encode('test');
const ARCHIVE_SHA256 = '9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08';
const INLINE_RESPONSE = {
  archive_sha256: ARCHIVE_SHA256,
  archive_bytes: ARCHIVE.byteLength,
  filename: 'agent-python.zip',
  zip_base64: 'dGVzdA==',
};
const PRESIGNED_URL =
  'https://acf-test-artifacts-us-east-1-123456789012.s3.us-east-1.amazonaws.com/'
  + 'python-exports/owner/archive.zip?X-Amz-Algorithm=AWS4-HMAC-SHA256'
  + '&X-Amz-Credential=TESTACCESS%2F20261027%2Fus-east-1%2Fs3%2Faws4_request'
  + '&X-Amz-Date=20261027T120000Z&X-Amz-Expires=3600&X-Amz-SignedHeaders=host'
  + `&X-Amz-Signature=${'a'.repeat(64)}`;
const URL_RESPONSE = {
  archive_sha256: ARCHIVE_SHA256,
  archive_bytes: ARCHIVE.byteLength,
  filename: 'agent-cfn.zip',
  download_url: PRESIGNED_URL,
};

const originalCreateObjectURL = Object.getOwnPropertyDescriptor(URL, 'createObjectURL');
const originalRevokeObjectURL = Object.getOwnPropertyDescriptor(URL, 'revokeObjectURL');
let createObjectURL: ReturnType<typeof vi.fn>;
let revokeObjectURL: ReturnType<typeof vi.fn>;
let click: ReturnType<typeof vi.spyOn>;
let clickedDownload: { href: string; filename: string; referrerPolicy: string } | null;
let fetchMock: ReturnType<typeof vi.fn>;

function archiveResponse(overrides: ResponseInit = {}): Response {
  return new Response(ARCHIVE.slice().buffer, {
    status: 200,
    headers: { 'Content-Length': String(ARCHIVE.byteLength) },
    ...overrides,
  });
}

async function expectRejectedWithoutDelivery(value: unknown): Promise<void> {
  await expect(downloadArtifact(value)).rejects.toThrow(ARTIFACT_INTEGRITY_ERROR);
  expect(createObjectURL).not.toHaveBeenCalled();
  expect(click).not.toHaveBeenCalled();
  expect(revokeObjectURL).not.toHaveBeenCalled();
}

beforeEach(() => {
  vi.stubGlobal('crypto', webcrypto as unknown as Crypto);
  fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
  createObjectURL = vi.fn(() => 'blob:verified-export');
  revokeObjectURL = vi.fn();
  Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: createObjectURL });
  Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: revokeObjectURL });
  clickedDownload = null;
  click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
    clickedDownload = {
      href: this.href,
      filename: this.download,
      referrerPolicy: this.referrerPolicy,
    };
  });
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  if (originalCreateObjectURL) Object.defineProperty(URL, 'createObjectURL', originalCreateObjectURL);
  else Reflect.deleteProperty(URL, 'createObjectURL');
  if (originalRevokeObjectURL) Object.defineProperty(URL, 'revokeObjectURL', originalRevokeObjectURL);
  else Reflect.deleteProperty(URL, 'revokeObjectURL');
  document.body.replaceChildren();
});

describe('downloadArtifact', () => {
  it('verifies and downloads a strict inline base64 response', async () => {
    await downloadArtifact(INLINE_RESPONSE);

    expect(fetchMock).not.toHaveBeenCalled();
    expect(createObjectURL).toHaveBeenCalledOnce();
    const blob = createObjectURL.mock.calls[0][0] as Blob;
    expect(blob.type).toBe('application/zip');
    expect(blob.size).toBe(ARCHIVE.byteLength);
    expect(clickedDownload).toEqual({
      href: 'blob:verified-export',
      filename: 'agent-python.zip',
      referrerPolicy: 'no-referrer',
    });
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:verified-export');
    expect(document.querySelector('a')).toBeNull();
  });

  it('fetches a presigned regional S3 URL without credentials or redirects, then verifies it', async () => {
    fetchMock.mockResolvedValue(archiveResponse());

    await downloadArtifact(URL_RESPONSE);

    expect(fetchMock).toHaveBeenCalledWith(PRESIGNED_URL, {
      method: 'GET',
      credentials: 'omit',
      cache: 'no-store',
      redirect: 'error',
      referrerPolicy: 'no-referrer',
      mode: 'cors',
    });
    expect(createObjectURL).toHaveBeenCalledOnce();
    expect(clickedDownload).toEqual({
      href: 'blob:verified-export',
      filename: 'agent-cfn.zip',
      referrerPolicy: 'no-referrer',
    });
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:verified-export');
  });

  it.each([
    ['digest comparison mutation guard', { ...INLINE_RESPONSE, archive_sha256: '0'.repeat(64) }],
    ['declared length mismatch', { ...INLINE_RESPONSE, archive_bytes: ARCHIVE.byteLength + 1 }],
    ['uppercase digest', { ...INLINE_RESPONSE, archive_sha256: ARCHIVE_SHA256.toUpperCase() }],
    ['short digest', { ...INLINE_RESPONSE, archive_sha256: ARCHIVE_SHA256.slice(1) }],
    ['non-numeric byte count', { ...INLINE_RESPONSE, archive_bytes: '4' }],
    ['zero byte count', { ...INLINE_RESPONSE, archive_bytes: 0 }],
    ['oversized byte count', { ...INLINE_RESPONSE, archive_bytes: 128 * 1024 * 1024 + 1 }],
    ['malformed base64', { ...INLINE_RESPONSE, zip_base64: 'dGVzdA=' }],
    ['non-canonical base64', { ...INLINE_RESPONSE, zip_base64: 'dGVzdB==' }],
    ['empty filename', { ...INLINE_RESPONSE, filename: '' }],
    ['unsafe filename', { ...INLINE_RESPONSE, filename: '../agent.zip' }],
    ['unexpected response field', { ...INLINE_RESPONSE, debug: true }],
    ['both sources', { ...INLINE_RESPONSE, download_url: PRESIGNED_URL }],
    ['neither source', {
      archive_sha256: ARCHIVE_SHA256,
      archive_bytes: ARCHIVE.byteLength,
      filename: 'agent.zip',
    }],
    ['non-S3 URL', { ...URL_RESPONSE, download_url: PRESIGNED_URL.replace('.s3.us-east-1.amazonaws.com', '.example.com') }],
    ['HTTP URL', { ...URL_RESPONSE, download_url: PRESIGNED_URL.replace('https:', 'http:') }],
  ])('rejects %s before creating a download', async (_label, value) => {
    await expectRejectedWithoutDelivery(value);
  });

  it('fails closed when Web Crypto is absent', async () => {
    vi.stubGlobal('crypto', undefined);
    await expectRejectedWithoutDelivery(INLINE_RESPONSE);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('fails closed on a fetch redirect without exposing its URL', async () => {
    const response = archiveResponse();
    Object.defineProperty(response, 'redirected', { value: true });
    fetchMock.mockResolvedValue(response);

    await expectRejectedWithoutDelivery(URL_RESPONSE);
  });

  it.each([
    ['HTTP error', archiveResponse({ status: 403 })],
    ['content-length mismatch', archiveResponse({ headers: { 'Content-Length': '5' } })],
  ])('fails closed on a fetched %s', async (_label, response) => {
    fetchMock.mockResolvedValue(response);
    await expectRejectedWithoutDelivery(URL_RESPONSE);
  });

  it('fails closed on a fetch transport error and returns only the generic message', async () => {
    fetchMock.mockRejectedValue(new Error(`request failed for ${PRESIGNED_URL}&secret=do-not-leak`));

    let message = '';
    try {
      await downloadArtifact(URL_RESPONSE);
    } catch (error) {
      message = error instanceof Error ? error.message : String(error);
    }
    expect(message).toBe(ARTIFACT_INTEGRITY_ERROR);
    expect(message).not.toContain('amazonaws.com');
    expect(message).not.toContain('secret');
    expect(createObjectURL).not.toHaveBeenCalled();
    expect(click).not.toHaveBeenCalled();
  });

  it('revokes the object URL and removes the anchor even if the click fails', async () => {
    click.mockImplementationOnce(() => {
      throw new Error('browser refused download');
    });

    await expect(downloadArtifact(INLINE_RESPONSE)).rejects.toThrow(ARTIFACT_INTEGRITY_ERROR);
    expect(createObjectURL).toHaveBeenCalledOnce();
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:verified-export');
    expect(document.querySelector('a')).toBeNull();
  });
});
