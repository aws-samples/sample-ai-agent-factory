const MAX_ARCHIVE_BYTES = 128 * 1024 * 1024;
const SHA256_HEX_PATTERN = /^[0-9a-f]{64}$/;
const BASE64_PATTERN = /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/;
const BUCKET_PATTERN = /^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$/;
const REGION_PATTERN = /^[a-z]{2}(?:-[a-z0-9]+)+-\d$/;
const NON_COMMERCIAL_REGION_PREFIXES = ['us-gov-', 'us-iso-', 'us-isob-', 'us-isof-', 'eu-isoe-'];

export const ARTIFACT_INTEGRITY_ERROR =
  'Export integrity verification failed. Retry the export; if it still fails, contact your administrator.';

type ArtifactMetadata = {
  archiveSha256: string;
  archiveBytes: number;
  filename: string;
  source: { kind: 'base64'; value: string } | { kind: 's3'; value: URL };
};

function invalidArtifact(): never {
  throw new Error(ARTIFACT_INTEGRITY_ERROR);
}

function hasExactKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  return actual.length === wanted.length && actual.every((key, index) => key === wanted[index]);
}

function parseFilename(value: unknown): string {
  const hasControlCharacter = typeof value === 'string'
    && Array.from(value).some((character) => {
      const codePoint = character.codePointAt(0) ?? 0;
      return codePoint <= 0x1f || codePoint === 0x7f;
    });
  if (
    typeof value !== 'string'
    || value.length === 0
    || value.length > 255
    || value !== value.trim()
    || value === '.'
    || value === '..'
    || /[\\/]/.test(value)
    || hasControlCharacter
  ) {
    invalidArtifact();
  }
  return value;
}

function requiredSingleQueryValue(url: URL, name: string): string {
  const values = url.searchParams.getAll(name);
  if (values.length !== 1 || values[0].length === 0) invalidArtifact();
  return values[0];
}

function parsePresignedS3Url(value: unknown): URL {
  if (typeof value !== 'string' || value.length === 0) invalidArtifact();

  let url: URL;
  try {
    url = new URL(value);
  } catch {
    invalidArtifact();
  }

  const hostParts = url.hostname.split('.');
  if (
    url.protocol !== 'https:'
    || url.username !== ''
    || url.password !== ''
    || url.port !== ''
    || url.hash !== ''
    || url.pathname === '/'
    || hostParts.length !== 5
    || hostParts[1] !== 's3'
    || hostParts[3] !== 'amazonaws'
    || hostParts[4] !== 'com'
    || !BUCKET_PATTERN.test(hostParts[0])
    || !REGION_PATTERN.test(hostParts[2])
    || NON_COMMERCIAL_REGION_PREFIXES.some((prefix) => hostParts[2].startsWith(prefix))
  ) {
    invalidArtifact();
  }

  const algorithm = requiredSingleQueryValue(url, 'X-Amz-Algorithm');
  const credential = requiredSingleQueryValue(url, 'X-Amz-Credential');
  const signedAt = requiredSingleQueryValue(url, 'X-Amz-Date');
  const expires = requiredSingleQueryValue(url, 'X-Amz-Expires');
  const signedHeaders = requiredSingleQueryValue(url, 'X-Amz-SignedHeaders');
  const signature = requiredSingleQueryValue(url, 'X-Amz-Signature');
  const credentialParts = credential.split('/');

  if (
    algorithm !== 'AWS4-HMAC-SHA256'
    || !/^\d{8}T\d{6}Z$/.test(signedAt)
    || !/^\d{1,4}$/.test(expires)
    || Number(expires) < 1
    || Number(expires) > 3600
    || signedHeaders !== 'host'
    || !SHA256_HEX_PATTERN.test(signature)
    || credentialParts.length !== 5
    || credentialParts[0].length === 0
    || credentialParts[1] !== signedAt.slice(0, 8)
    || credentialParts[2] !== hostParts[2]
    || credentialParts[3] !== 's3'
    || credentialParts[4] !== 'aws4_request'
  ) {
    invalidArtifact();
  }

  return url;
}

function parseMetadata(value: unknown): ArtifactMetadata {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) invalidArtifact();
  const record = value as Record<string, unknown>;
  const hasBase64 = Object.prototype.hasOwnProperty.call(record, 'zip_base64');
  const hasUrl = Object.prototype.hasOwnProperty.call(record, 'download_url');
  if (hasBase64 === hasUrl) invalidArtifact();

  const sourceKey = hasBase64 ? 'zip_base64' : 'download_url';
  if (!hasExactKeys(record, ['archive_sha256', 'archive_bytes', 'filename', sourceKey])) {
    invalidArtifact();
  }
  if (typeof record.archive_sha256 !== 'string' || !SHA256_HEX_PATTERN.test(record.archive_sha256)) {
    invalidArtifact();
  }
  if (
    typeof record.archive_bytes !== 'number'
    || !Number.isSafeInteger(record.archive_bytes)
    || record.archive_bytes < 1
    || record.archive_bytes > MAX_ARCHIVE_BYTES
  ) {
    invalidArtifact();
  }

  return {
    archiveSha256: record.archive_sha256,
    archiveBytes: record.archive_bytes,
    filename: parseFilename(record.filename),
    source: hasBase64
      ? { kind: 'base64', value: parseBase64(record.zip_base64, record.archive_bytes) }
      : { kind: 's3', value: parsePresignedS3Url(record.download_url) },
  };
}

function parseBase64(value: unknown, expectedBytes: number): string {
  if (typeof value !== 'string' || value.length === 0) invalidArtifact();
  const maximumEncodedLength = 4 * Math.ceil(MAX_ARCHIVE_BYTES / 3);
  const expectedEncodedLength = 4 * Math.ceil(expectedBytes / 3);
  if (
    value.length > maximumEncodedLength
    || value.length !== expectedEncodedLength
    || value.length % 4 !== 0
    || !BASE64_PATTERN.test(value)
  ) {
    invalidArtifact();
  }
  return value;
}

function decodeBase64(value: string): Uint8Array<ArrayBuffer> {
  let decoded: string;
  try {
    decoded = atob(value);
  } catch {
    invalidArtifact();
  }
  if (btoa(decoded) !== value) invalidArtifact();
  return Uint8Array.from(decoded, (character) => character.charCodeAt(0));
}

function parseContentLength(response: Response, expectedBytes: number): void {
  const value = response.headers.get('content-length');
  if (value === null) return;
  if (!/^\d+$/.test(value) || Number(value) !== expectedBytes) invalidArtifact();
}

async function readBoundedBody(response: Response, expectedBytes: number): Promise<Uint8Array<ArrayBuffer>> {
  parseContentLength(response, expectedBytes);
  if (!response.body) invalidArtifact();

  const reader = response.body.getReader();
  const bytes = new Uint8Array(expectedBytes);
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (total + value.byteLength > expectedBytes || total + value.byteLength > MAX_ARCHIVE_BYTES) {
        await reader.cancel().catch(() => undefined);
        invalidArtifact();
      }
      bytes.set(value, total);
      total += value.byteLength;
    }
  } catch {
    invalidArtifact();
  } finally {
    reader.releaseLock();
  }
  if (total !== expectedBytes) invalidArtifact();
  return bytes;
}

async function fetchS3Bytes(url: URL, expectedBytes: number): Promise<Uint8Array<ArrayBuffer>> {
  const response = await fetch(url.href, {
    method: 'GET',
    credentials: 'omit',
    cache: 'no-store',
    redirect: 'error',
    referrerPolicy: 'no-referrer',
    mode: 'cors',
  });
  if (!response.ok || response.status !== 200 || response.redirected || response.type === 'opaqueredirect') {
    invalidArtifact();
  }
  return readBoundedBody(response, expectedBytes);
}

function bytesToHex(bytes: ArrayBuffer): string {
  return Array.from(new Uint8Array(bytes), (byte) => byte.toString(16).padStart(2, '0')).join('');
}

async function verifyBytes(bytes: Uint8Array<ArrayBuffer>, metadata: ArtifactMetadata): Promise<Uint8Array<ArrayBuffer>> {
  if (bytes.byteLength !== metadata.archiveBytes || bytes.byteLength < 1 || bytes.byteLength > MAX_ARCHIVE_BYTES) {
    invalidArtifact();
  }
  const subtle = globalThis.crypto?.subtle;
  if (!subtle || typeof subtle.digest !== 'function') invalidArtifact();
  // Digest a fresh typed-array VIEW, not the backing ArrayBuffer. A buffer that was created
  // in another realm (a DOM response body under a test runtime, an iframe) fails WebIDL's
  // BufferSource check in some engines even though the bytes are fine; a view constructed
  // here is always this realm's, and the copy also severs any alias the caller still holds.
  const view = new Uint8Array(bytes);
  const digest = await subtle.digest('SHA-256', view);
  if (bytesToHex(digest) !== metadata.archiveSha256) invalidArtifact();
  return view;
}

function saveBytes(bytes: Uint8Array<ArrayBuffer>, filename: string): void {
  let objectUrl: string | null = null;
  let anchor: HTMLAnchorElement | null = null;
  try {
    objectUrl = URL.createObjectURL(new Blob([bytes], { type: 'application/zip' }));
    anchor = document.createElement('a');
    anchor.href = objectUrl;
    anchor.download = filename;
    anchor.referrerPolicy = 'no-referrer';
    anchor.hidden = true;
    document.body.appendChild(anchor);
    anchor.click();
  } finally {
    anchor?.remove();
    if (objectUrl !== null) URL.revokeObjectURL(objectUrl);
  }
}

/** Validate, retrieve, hash, and only then offer one export archive to the browser. */
export async function downloadArtifact(value: unknown): Promise<void> {
  try {
    if (!globalThis.crypto?.subtle || typeof globalThis.crypto.subtle.digest !== 'function') invalidArtifact();
    const metadata = parseMetadata(value);
    const bytes = metadata.source.kind === 'base64'
      ? decodeBase64(metadata.source.value)
      : await fetchS3Bytes(metadata.source.value, metadata.archiveBytes);
    const verified = await verifyBytes(bytes, metadata);
    saveBytes(verified, metadata.filename);
  } catch {
    throw new Error(ARTIFACT_INTEGRITY_ERROR);
  }
}
