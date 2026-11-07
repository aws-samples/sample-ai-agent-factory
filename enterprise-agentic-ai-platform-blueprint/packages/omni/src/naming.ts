/**
 * Naming and Region rules for CloudWatch Omni resources, taken from the
 * CloudWatch Omni user guide (domain-name requirements, space-name pattern) and
 * the Region availability announcements of 2026-09 and 2026-10.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

/** Regions where a CloudWatch Omni space can be hosted (GA 2026-09, expansion 2026-10). */
export const OMNI_HOSTING_REGIONS: readonly string[] = [
  'us-east-1',
  'us-west-2',
  'eu-west-1',
  'eu-central-1',
  'eu-west-2',
  'ca-central-1',
  'ap-south-1',
  'ap-southeast-1',
  'ap-southeast-2',
  'ap-northeast-1',
  'sa-east-1',
];

const RESERVED_DOMAIN_PREFIXES = ['aws-', 'amazon-', 'cloudwatch-', 'omni-'];

/** `https://<domain-name>.cloudwatch-omni.global.app.aws` */
export function omniDomainUrl(domainName: string): string {
  return `https://${domainName}.cloudwatch-omni.global.app.aws`;
}

/**
 * Domain names: 3-63 chars of lowercase letters, digits and hyphens; start and
 * end alphanumeric; no consecutive hyphens; reserved prefixes refused. The
 * service also refuses AWS service names and Region-like names, which cannot
 * be checked exhaustively at synth, so a live `CreateDomainForOrganization`
 * remains the final gate.
 */
export function assertOmniDomainName(name: string): void {
  if (!/^[a-z0-9]+(-[a-z0-9]+)*$/.test(name) || name.length < 3 || name.length > 63) {
    throw new Error(
      `CloudWatch Omni domain name '${name}' is invalid: use 3-63 lowercase letters, digits and single hyphens, starting and ending with a letter or digit.`,
    );
  }
  for (const prefix of RESERVED_DOMAIN_PREFIXES) {
    if (name.startsWith(prefix)) {
      throw new Error(`CloudWatch Omni domain name '${name}' uses the reserved prefix '${prefix}'.`);
    }
  }
  if (/^[a-z]{2}-[a-z]+-\d$/.test(name)) {
    throw new Error(`CloudWatch Omni domain name '${name}' looks like an AWS Region name, which is reserved.`);
  }
}

/** Space names: 3-64 chars, `^[a-z0-9]+(-[a-z0-9]+)*$`. */
export function assertOmniSpaceName(name: string): void {
  if (!/^[a-z0-9]+(-[a-z0-9]+)*$/.test(name) || name.length < 3 || name.length > 64) {
    throw new Error(
      `CloudWatch Omni space name '${name}' is invalid: use 3-64 lowercase letters, digits and single hyphens.`,
    );
  }
}

/**
 * Fail closed on a Region that cannot host a space. Paris (eu-west-3), Milan,
 * Spain, Zurich and Stockholm are not Omni hosting Regions as of 2026-11; a
 * cell there can only centralize into another Region's space, which is a
 * residency decision the customer must take explicitly (ADR-19).
 */
export function assertOmniHostingRegion(region: string): void {
  if (!OMNI_HOSTING_REGIONS.includes(region)) {
    throw new Error(
      `CloudWatch Omni cannot host a space in ${region}. Hosting Regions as of 2026-11: ${OMNI_HOSTING_REGIONS.join(', ')}. ` +
        'Host the space in a supported Region and centralize this Region into it only after the residency and data-transfer decision is recorded (ADR-19).',
    );
  }
}

export function assertAccountId(label: string, accountId: string): void {
  if (!/^\d{12}$/.test(accountId)) {
    throw new Error(`${label} '${accountId}' must contain exactly 12 digits.`);
  }
}
