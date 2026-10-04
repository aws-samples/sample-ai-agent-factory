/**
 * Optional, CloudFormation-only resource naming profile.
 *
 * A blank prefix leaves the profile unset and preserves the legacy physical-name
 * formulas. The backend may still tighten the generated parameter limits when an
 * otherwise accepted value would overflow an emitted AWS resource name. When configured,
 * one validated profile is applied to resource names and every coupled IAM ARN, OAuth
 * scope, output and log qualifier. Advanced templates are deliberately JSON: they are
 * an expert escape hatch, while the common ECB-style case remains one field.
 */

import { useState } from 'react';

const PREFIX_PATTERN = /^[a-z][a-z0-9]{0,11}$/;
const OVERRIDE_EXAMPLE = `{
  "gateway": "{prefix}-{deployment}-gw",
  "runtime": "{prefix}_{deployment}_agent"
}`;

const RESOURCE_FAMILIES = new Set([
  'cognitoUserPool',
  'cognitoResourceServer',
  'cognitoClient',
  'cognitoDomain',
  'gateway',
  'gatewayTarget',
  'toolLambda',
  'customToolLambda',
  'knowledgeBase',
  'knowledgeBaseDataSource',
  'knowledgeBaseToolLambda',
  'vectorBucket',
  'vectorIndex',
  'memory',
  'policyEngine',
  'policy',
  'mcpCognitoUserPool',
  'mcpCognitoResourceServer',
  'mcpCognitoClient',
  'mcpCognitoDomain',
  'mcpCredentialProvider',
  'mcpRuntime',
  'mcpEndpoint',
  'evaluation',
  'guardrail',
  'runtime',
  'runtimeEndpoint',
  'runtimeRole',
  'gatewayRole',
  'toolLambdaRole',
  'knowledgeBaseToolRole',
  'knowledgeBaseRole',
  'memoryRole',
  'mcpRuntimeRole',
  'evaluationRole',
]);

const COMPONENT_FAMILIES = new Set([
  'gatewayTarget',
  'toolLambda',
  'customToolLambda',
  'policy',
]);
const STACK_UNIQUE_FAMILIES = new Set([
  'cognitoDomain',
  'mcpCognitoDomain',
  'vectorBucket',
]);
const ROLE_FAMILIES = new Set([
  'runtimeRole',
  'gatewayRole',
  'toolLambdaRole',
  'knowledgeBaseToolRole',
  'knowledgeBaseRole',
  'memoryRole',
  'mcpRuntimeRole',
  'evaluationRole',
]);

export interface CfnNamingProfile {
  prefix: string;
  resourceNames?: Record<string, string>;
}

export interface ResourceNamingState {
  prefix: string;
  rawResourceNames: string;
  profile: CfnNamingProfile | null;
  error: string | null;
}

function parseResourceNames(raw: string): {
  resourceNames?: Record<string, string>;
  error?: string;
} {
  if (!raw.trim()) return {};

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return { error: 'Advanced resource templates must be valid JSON.' };
  }

  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { error: 'Advanced resource templates must be a JSON object.' };
  }

  const entries = Object.entries(parsed);
  // The backend currently exposes 35 families, so a complete customer profile
  // must fit. 64 leaves controlled room for the contract to grow.
  if (entries.length > 64) {
    return { error: 'Advanced resource templates accept at most 64 overrides.' };
  }

  const resourceNames: Record<string, string> = {};
  for (const [family, value] of entries) {
    if (!RESOURCE_FAMILIES.has(family)) {
      return { error: `Unknown resource family "${family}".` };
    }
    if (typeof value !== 'string' || value.length === 0 || value.length > 160) {
      return { error: `Template "${family}" must be a non-empty string of at most 160 characters.` };
    }
    if ([...value].some((character) => {
      const code = character.charCodeAt(0);
      return code < 32 || code > 126;
    })) {
      return { error: `Template "${family}" must contain printable ASCII characters only.` };
    }

    const allowedPlaceholders = new Set(['prefix', 'deployment']);
    if (COMPONENT_FAMILIES.has(family)) allowedPlaceholders.add('component');
    if (STACK_UNIQUE_FAMILIES.has(family) || ROLE_FAMILIES.has(family)) {
      allowedPlaceholders.add('suffix');
    }
    const placeholders = [...value.matchAll(/\{([^{}]*)\}/g)];
    const remainder = value.replace(/\{[^{}]*\}/g, '');
    if (/[{}]/.test(remainder)) {
      return { error: `Template "${family}" contains an unmatched brace.` };
    }
    for (const match of placeholders) {
      const placeholder = match[1];
      if (!allowedPlaceholders.has(placeholder)) {
        return {
          error: `Template "${family}" uses unsupported placeholder {${placeholder}}.`,
        };
      }
    }
    if (!value.includes('{deployment}')) {
      return { error: `Template "${family}" must include {deployment}.` };
    }
    if (COMPONENT_FAMILIES.has(family) && !value.includes('{component}')) {
      return { error: `Template "${family}" must include {component}.` };
    }
    if ((STACK_UNIQUE_FAMILIES.has(family) || ROLE_FAMILIES.has(family)) && !value.includes('{suffix}')) {
      return { error: `Template "${family}" must include {suffix}.` };
    }
    resourceNames[family] = value;
  }

  return { resourceNames };
}

function buildState(prefix: string, rawResourceNames: string): ResourceNamingState {
  const draft = { prefix, rawResourceNames };
  if (!prefix && !rawResourceNames.trim()) {
    return { ...draft, profile: null, error: null };
  }
  if (!prefix) {
    return {
      ...draft,
      profile: null,
      error: 'Enter a naming prefix before adding advanced resource templates.',
    };
  }
  if (!PREFIX_PATTERN.test(prefix)) {
    return {
      ...draft,
      profile: null,
      error: 'Prefix must start with a lowercase letter and contain 1–12 lowercase letters or digits.',
    };
  }

  const parsed = parseResourceNames(rawResourceNames);
  if (parsed.error) {
    return { ...draft, profile: null, error: parsed.error };
  }
  return {
    ...draft,
    profile: {
      prefix,
      ...(parsed.resourceNames && Object.keys(parsed.resourceNames).length > 0
        ? { resourceNames: parsed.resourceNames }
        : {}),
    },
    error: null,
  };
}

// eslint-disable-next-line react-refresh/only-export-components
export function resourceNamingStateFromProfile(
  profile: CfnNamingProfile | null,
): ResourceNamingState {
  if (!profile) {
    return {
      prefix: '',
      rawResourceNames: '',
      profile: null,
      error: null,
    };
  }
  return buildState(
    profile.prefix,
    profile.resourceNames && Object.keys(profile.resourceNames).length > 0
      ? JSON.stringify(profile.resourceNames, null, 2)
      : '',
  );
}

export function ResourceNamingFields({
  value,
  onChange,
}: {
  value: ResourceNamingState;
  onChange: (state: ResourceNamingState) => void;
}) {
  // Only disclosure state is local. The actual draft lives in DeployPanel so a
  // deploy/error transition can unmount this section without erasing what the
  // operator entered before they download the bundle.
  const [showAdvanced, setShowAdvanced] = useState(Boolean(value.rawResourceNames));

  return (
    <div className="rounded-lg border border-white/10 p-3 space-y-3 no-darkmap">
      <div className="flex items-center justify-between gap-3">
        <span className="text-sm font-medium">CloudFormation naming</span>
        <span
          className="rounded px-2 py-0.5 text-[10px] font-medium"
          style={{
            color: 'var(--color-aws-blue-hover)',
            background: 'color-mix(in srgb, var(--color-aws-blue) 12%, transparent)',
          }}
        >
          CFN export only
        </span>
      </div>

      <p id="cfn-naming-help" className="text-xs text-[#5f6b7a]">
        Optional. Add a customer prefix such as <code>ecb</code>; leave blank to preserve
        the current resource names. Live deploy and Python export are unchanged.
      </p>

      <label className="block text-xs" htmlFor="cfn-naming-prefix">
        <span className="opacity-70">Naming prefix</span>
        <input
          id="cfn-naming-prefix"
          type="text"
          className="mt-1 w-full rounded bg-black/20 border border-white/10 px-2 py-1 text-sm"
          value={value.prefix}
          maxLength={12}
          placeholder="ecb"
          autoComplete="off"
          spellCheck={false}
          aria-describedby={`cfn-naming-help${value.error ? ' cfn-naming-error' : ''}`}
          aria-invalid={value.error ? 'true' : undefined}
          onChange={(event) => {
            onChange(buildState(event.target.value, value.rawResourceNames));
          }}
        />
      </label>

      <button
        type="button"
        className="text-xs font-medium text-blue-700 hover:text-blue-800"
        aria-expanded={showAdvanced}
        aria-controls="cfn-naming-advanced"
        onClick={() => setShowAdvanced((value) => !value)}
      >
        {showAdvanced ? 'Hide advanced resource templates' : 'Advanced resource templates'}
      </button>

      {showAdvanced && (
        <div id="cfn-naming-advanced" className="space-y-1.5">
          {/* The label must NOT wrap the textarea: a wrapping label's text includes the
              control's content, so once templates are saved the field's accessible name
              became "Per-family templates (JSON){...json...}" (measured live, 2026-09-28;
              the same defect the tag-profile select had). Sibling label + htmlFor keeps the
              name exact whatever the field holds. */}
          <label className="block text-xs opacity-70" htmlFor="cfn-resource-name-templates">
            Per-family templates (JSON)
          </label>
          <textarea
            id="cfn-resource-name-templates"
            className="mt-1 min-h-28 w-full rounded bg-black/20 border border-white/10 px-2 py-1.5 font-mono text-xs"
            value={value.rawResourceNames}
            placeholder={OVERRIDE_EXAMPLE}
            spellCheck={false}
            aria-describedby={`cfn-template-help${value.error ? ' cfn-naming-error' : ''}`}
            aria-invalid={value.error && value.rawResourceNames.trim() ? 'true' : undefined}
            onChange={(event) => {
              onChange(buildState(value.prefix, event.target.value));
            }}
          />
          <p id="cfn-template-help" className="text-[11px] text-[#5f6b7a]">
            Use supported families and placeholders. Every template needs {'{deployment}'};
            component names need {'{component}'}, and stack-unique names need {'{suffix}'}.
          </p>
        </div>
      )}

      {value.error && (
        <p id="cfn-naming-error" role="alert" className="text-xs text-red-600">
          {value.error}
        </p>
      )}
    </div>
  );
}
