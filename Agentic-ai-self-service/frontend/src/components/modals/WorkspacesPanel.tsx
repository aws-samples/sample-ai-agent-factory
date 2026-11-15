/**
 * WorkspacesPanel — Admin → Workspaces (plan §3.B, implementation step 5).
 *
 * Lists every workspace policy, creates or edits one from a preset (Locked-down, Standard,
 * Developer) and deletes one. The backend is the control: the panel renders only while the
 * `workspacePolicy` flag is on (off, its routes are a 404) and only for admins; a refusal from the
 * strict schema (422), the id guard (400) or the scope gate (403) is shown verbatim, never
 * reinterpreted. It also warns when no `default` policy exists, because with the flag on every
 * deploy is refused (503) until one does.
 *
 * Saving is never a blind replacement. Editing fetches the policy as the server holds it NOW, with its
 * version token (`ETag`), and the save names that version (`If-Match`); a new policy is created only if
 * none exists (`If-None-Match: *`). When a colleague saved first, or deleted and recreated the policy, the
 * backend answers `412`: the panel shows
 * that refusal verbatim, keeps the admin's form, and offers to reload the current version so the change
 * can be reapplied on top of it rather than over it (a tightened approver routing cannot be undone by a
 * stale form).
 */

import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { getApiClient, getErrorMessage } from '../../services/api';
import { API_BASE_URL } from '../../services/api/client';
import {
  ALLOWED_DATA_CLASSES,
  ALLOWED_TOOL_RISK_CLASSES,
  CHANNEL_TYPES,
  DEFAULT_WORKSPACE_ID,
  GUARDRAIL_PROFILES,
  RISK_TIERS,
  isRevisionConflict,
  type GuardrailProfile,
  type PlatformVersion,
  type PutPrecondition,
  type RiskTier,
  type WorkspacePolicyDocument,
} from '../../services/api/workspacePolicies';
import { useFeatureFlags } from '../../hooks/useFeatureFlags';
import { FeatureGate } from '../FeatureGate';
import { WORKSPACE_PRESETS, applyPreset, emptyPolicy, type WorkspacePresetId } from './workspacePresets';
import {
  REDUCED_REVIEW_ACKNOWLEDGEMENT,
  documentFromForm,
  formFromDocument,
  presetInputFromForm,
  reviewersFromRouting,
  routingFromReviewers,
  summarizeAllowed,
  summarizeApproval,
  type WorkspacePolicyForm,
} from './workspacePolicyForm';

export interface WorkspacesPanelProps {
  /** Only admins see the panel; the backend's `admin` scope is the real gate. */
  isAdmin: boolean;
}

export function WorkspacesPanel({ isAdmin }: WorkspacesPanelProps) {
  const { state } = useFeatureFlags(API_BASE_URL);
  if (!isAdmin) {
    return null;
  }
  return (
    <FeatureGate state={state} flag="workspacePolicy">
      <WorkspacesPanelBody />
    </FeatureGate>
  );
}

const INPUT = 'rounded border border-white/20 bg-transparent px-2 py-1 text-xs w-full';
const BUTTON = 'text-[11px] px-2 py-0.5 rounded border border-white/20 hover:bg-white/10 disabled:opacity-40';
const PRIMARY = 'text-[11px] px-2.5 py-1 rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-40';

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-0.5 text-[11px]">
      <span className="font-medium">{label}</span>
      {children}
      {hint && <span className="text-[10px] text-gray-500">{hint}</span>}
    </label>
  );
}

function ChoiceGroup<T extends string>({
  label,
  hint,
  options,
  selected,
  onChange,
}: {
  label: string;
  hint?: string;
  options: readonly T[];
  selected: T[];
  onChange: (next: T[]) => void;
}) {
  return (
    <fieldset className="text-[11px]">
      <legend className="font-medium">{label}</legend>
      <div className="flex flex-wrap gap-2 mt-0.5">
        {options.map((option) => (
          <label key={option} className="flex items-center gap-1">
            <input
              type="checkbox"
              checked={selected.includes(option)}
              onChange={(event) =>
                onChange(
                  event.target.checked ? [...selected, option] : selected.filter((item) => item !== option),
                )
              }
            />
            {option}
          </label>
        ))}
      </div>
      {hint && <div className="text-[10px] text-gray-500">{hint}</div>}
    </fieldset>
  );
}

function membersLine(policy: WorkspacePolicyDocument): string {
  if (policy.workspaceId === DEFAULT_WORKSPACE_ID) {
    return 'everyone who is in no other workspace';
  }
  return policy.memberGroups.length ? policy.memberGroups.join(', ') : 'no member groups yet, so it governs nobody';
}

function WorkspacesPanelBody() {
  const [policies, setPolicies] = useState<WorkspacePolicyDocument[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [form, setForm] = useState<WorkspacePolicyForm | null>(null);
  const [editingExisting, setEditingExisting] = useState(false);
  /** The version token the open form was loaded from; the save names it. `null` while creating. */
  const [version, setVersion] = useState<string | null>(null);
  /** The workspace whose save was refused as stale (412); offers "Reload current version". */
  const [conflict, setConflict] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setPolicies(await getApiClient().listWorkspacePolicies());
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const update = <K extends keyof WorkspacePolicyForm>(key: K, value: WorkspacePolicyForm[K]) => {
    setForm((current) => (current ? { ...current, [key]: value } : current));
  };

  const startNew = (workspaceId = '', name = '', preset: WorkspacePresetId | null = null) => {
    const document = preset
      ? applyPreset(preset, {
          workspaceId,
          name,
          memberGroups: [],
          adminGroups: [],
          approverGroups: [],
          securityGroups: [],
        })
      : emptyPolicy(workspaceId, name);
    setForm({ ...formFromDocument(document), workspaceId, name });
    setEditingExisting(false);
    setVersion(null);
    setConflict(null);
    setNotice(
      preset
        ? 'Started from the Standard preset. Enter the approver and security groups before saving: a policy with no reviewers auto-approves.'
        : null,
    );
    setError(null);
  };

  /**
   * The reviewer inputs and the medium/high routing are one state seen two ways: editing either side
   * re-derives the other, so a group typed anywhere is never dropped on save (a `g-privacy` added to
   * the high routing survives a later change of the approver group).
   */
  const setReviewers = (field: 'approverGroups' | 'securityGroups', value: string) => {
    setForm((current) => {
      if (!current) return current;
      const next = { ...current, [field]: value };
      return { ...next, ...routingFromReviewers(next.approverGroups, next.securityGroups) };
    });
  };
  const setRouting = (field: 'approvalMedium' | 'approvalHigh', value: string) => {
    setForm((current) => {
      if (!current) return current;
      const next = { ...current, [field]: value };
      return { ...next, ...reviewersFromRouting(next.approvalMedium, next.approvalHigh) };
    });
  };

  /**
   * Edit what the server holds NOW, not the list's snapshot: the fetch also yields the version the
   * save must name, so a colleague's change made since the list loaded is seen rather than overwritten.
   */
  const startEdit = async (workspaceId: string) => {
    setBusy(true);
    setError(null);
    setConflict(null);
    setNotice(null);
    try {
      const stored = await getApiClient().getWorkspacePolicy(workspaceId);
      setForm(formFromDocument(stored.policy));
      setVersion(stored.version);
      setEditingExisting(true);
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const applyPresetToForm = (id: WorkspacePresetId) => {
    if (!form) return;
    const preset = WORKSPACE_PRESETS.find((candidate) => candidate.id === id);
    const document = applyPreset(id, presetInputFromForm(form));
    // Keep the reviewer inputs the admin typed; the document carries their effect.
    const reviewed = id !== 'developer';
    const reviewersMissing = reviewed && (!document.approval.medium.length || !document.approval.high.length);
    setForm({
      ...formFromDocument(document, { approverGroups: form.approverGroups, securityGroups: form.securityGroups }),
      workspaceId: form.workspaceId,
      name: form.name,
      // Developer is the one preset that auto-approves on purpose; every other empty routing must be
      // acknowledged explicitly before it can be saved.
      reducedReviewAcknowledged: !reviewed,
    });
    setNotice(
      reviewersMissing
        ? `Applied the ${preset?.label ?? id} preset. It routes medium risk to the approver groups and high risk to security, so enter those groups before saving.`
        : `Applied the ${preset?.label ?? id} preset. Review the fields, then save.`,
    );
    setError(null);
  };

  const save = async () => {
    if (!form) return;
    if (editingExisting && version === null) {
      // Cannot happen through the UI (editing always starts from a fetch); refuse rather than save blind.
      setError('The version of this policy is unknown, so it cannot be saved safely. Reload it and try again.');
      return;
    }
    setBusy(true);
    setError(null);
    setConflict(null);
    try {
      const document = documentFromForm(form);
      const precondition: PutPrecondition = editingExisting && version !== null ? { version } : 'create';
      const stored = await getApiClient().putWorkspacePolicy(document, precondition);
      setNotice(`Saved the policy for workspace "${stored.policy.workspaceId}".`);
      setForm(null);
      setVersion(null);
      await load();
    } catch (e) {
      // A 412 is a colleague's save (or a delete and recreate), not a defect in the form: say so verbatim and offer the current version.
      setError(getErrorMessage(e));
      if (isRevisionConflict(e)) {
        setConflict(form.workspaceId.trim());
      }
    } finally {
      setBusy(false);
    }
  };

  const remove = async (workspaceId: string) => {
    setBusy(true);
    setError(null);
    try {
      await getApiClient().deleteWorkspacePolicy(workspaceId);
      setNotice(`Deleted the policy for workspace "${workspaceId}".`);
      setConfirmingDelete(null);
      await load();
    } catch (e) {
      setError(getErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const hasDefault = policies.some((policy) => policy.workspaceId === DEFAULT_WORKSPACE_ID);

  return (
    <section
      aria-labelledby="workspaces-panel-title"
      className="rounded-lg border border-white/10 p-3 space-y-3 no-darkmap"
      data-testid="workspaces-panel"
    >
      <div className="flex items-center justify-between">
        <span id="workspaces-panel-title" className="text-sm font-medium">
          Workspaces (policy and risk-tiered admission)
        </span>
        <div className="flex gap-2">
          <button type="button" className={BUTTON} onClick={() => void load()} disabled={loading || busy}>
            {loading ? 'Loading…' : 'Refresh'}
          </button>
          <button type="button" className={PRIMARY} onClick={() => startNew()} disabled={busy}>
            New workspace
          </button>
        </div>
      </div>

      <p className="text-[11px] text-gray-500">
        A policy says what a workspace may use and which groups approve each risk tier. The workspace
        that governs an agent follows its deployer's Cognito groups; everyone else is governed by{' '}
        <code>default</code>.
      </p>

      {error && (
        <div role="alert" className="text-xs text-red-400 flex items-start justify-between gap-2">
          <span>{error}</span>
          {conflict && (
            <button
              type="button"
              className={`${BUTTON} shrink-0`}
              onClick={() => void startEdit(conflict)}
              disabled={busy}
              title="Fetch the policy as it is now; your unsaved edits are discarded and can be reapplied."
            >
              Reload current version
            </button>
          )}
        </div>
      )}
      {notice && (
        <div role="status" className="text-xs text-emerald-500">
          {notice}
        </div>
      )}

      {!loading && !error && !hasDefault && (
        <div role="alert" className="text-[11px] text-amber-500 flex items-center justify-between gap-2">
          <span>
            No <code>default</code> policy is stored. While the <code>workspacePolicy</code> flag is on,
            every deploy is refused (503) until one exists.
          </span>
          <button
            type="button"
            className={BUTTON}
            onClick={() => startNew(DEFAULT_WORKSPACE_ID, 'Default', 'standard')}
          >
            Create the default policy
          </button>
        </div>
      )}

      {policies.length === 0 && !loading ? (
        <p className="text-[11px] text-gray-500">No workspace policies yet.</p>
      ) : (
        <ul className="space-y-1" aria-label="Workspace policies">
          {policies.map((policy) => (
            <li
              key={policy.workspaceId}
              className="text-[11px] px-2 py-1.5 rounded bg-white/10 space-y-0.5"
              data-testid={`workspace-${policy.workspaceId}`}
            >
              <div className="flex items-center justify-between gap-2">
                <div>
                  <span className="font-medium">{policy.name}</span>{' '}
                  <span className="font-mono text-[10px] text-gray-500">{policy.workspaceId}</span>
                </div>
                <div className="flex gap-1">
                  <button
                    type="button"
                    className={BUTTON}
                    onClick={() => void startEdit(policy.workspaceId)}
                    disabled={busy}
                  >
                    Edit
                  </button>
                  {confirmingDelete === policy.workspaceId ? (
                    <>
                      <button
                        type="button"
                        className={`${BUTTON} text-red-400`}
                        onClick={() => void remove(policy.workspaceId)}
                        disabled={busy}
                      >
                        Confirm delete
                      </button>
                      <button type="button" className={BUTTON} onClick={() => setConfirmingDelete(null)}>
                        Cancel
                      </button>
                    </>
                  ) : (
                    <button
                      type="button"
                      className={BUTTON}
                      onClick={() => setConfirmingDelete(policy.workspaceId)}
                      disabled={busy}
                    >
                      Delete
                    </button>
                  )}
                </div>
              </div>
              <div className="text-[10px] text-gray-500">Members: {membersLine(policy)}</div>
              <div className="text-[10px] text-gray-500">Allowed: {summarizeAllowed(policy.allowed)}</div>
              <div className="text-[10px] text-gray-500">Approval: {summarizeApproval(policy.approval)}</div>
            </li>
          ))}
        </ul>
      )}

      {form && (
        <form
          className="space-y-3 border-t border-white/10 pt-3"
          aria-label={editingExisting ? 'Edit workspace policy' : 'New workspace policy'}
          onSubmit={(event) => {
            event.preventDefault();
            void save();
          }}
        >
          {editingExisting && version !== null && (
            <p className="text-[10px] text-gray-500" data-testid="workspace-policy-version">
              Editing version <code>{version}</code> of this policy. Saving replaces exactly that version; if
              someone else saves first, or deletes and recreates the policy, the save is refused and you can
              reload their version.
            </p>
          )}
          <div className="flex flex-wrap gap-1 items-center">
            <span className="text-[11px] font-medium">Preset:</span>
            {WORKSPACE_PRESETS.map((preset) => (
              <button
                key={preset.id}
                type="button"
                className={BUTTON}
                title={preset.summary}
                onClick={() => applyPresetToForm(preset.id)}
                disabled={busy}
              >
                {preset.label}
              </button>
            ))}
            <span className="text-[10px] text-gray-500">
              Presets route medium risk to the approver groups and high risk to security and approvers.
            </span>
          </div>

          <div className="grid grid-cols-2 gap-2">
            <Field label="Workspace id" hint="Letters, digits, . _ : -; cannot change after saving.">
              <input
                className={INPUT}
                value={form.workspaceId}
                onChange={(event) => update('workspaceId', event.target.value)}
                disabled={editingExisting}
                required
              />
            </Field>
            <Field label="Name">
              <input className={INPUT} value={form.name} onChange={(event) => update('name', event.target.value)} required />
            </Field>
            <Field label="Member groups" hint="Cognito groups whose members deploy into this workspace.">
              <input
                className={INPUT}
                value={form.memberGroups}
                onChange={(event) => update('memberGroups', event.target.value)}
                placeholder="g-finance-builders"
                disabled={form.workspaceId.trim() === DEFAULT_WORKSPACE_ID}
              />
            </Field>
            <Field label="Admin groups">
              <input
                className={INPUT}
                value={form.adminGroups}
                onChange={(event) => update('adminGroups', event.target.value)}
                placeholder="g-admins-super"
              />
            </Field>
            <Field label="Approver groups (fills medium and high)" hint="Review medium-risk agents.">
              <input
                className={INPUT}
                value={form.approverGroups}
                onChange={(event) => setReviewers('approverGroups', event.target.value)}
                placeholder="g-approvers"
              />
            </Field>
            <Field label="Security groups (fills high)" hint="Must also clear high-risk agents.">
              <input
                className={INPUT}
                value={form.securityGroups}
                onChange={(event) => setReviewers('securityGroups', event.target.value)}
                placeholder="g-security"
              />
            </Field>
          </div>

          <div className="grid grid-cols-2 gap-2">
            <Field label="Allowed providers" hint="Empty = unconstrained.">
              <input className={INPUT} value={form.providers} onChange={(event) => update('providers', event.target.value)} placeholder="bedrock" />
            </Field>
            <Field label="Allowed models" hint="Empty = unconstrained.">
              <input className={INPUT} value={form.models} onChange={(event) => update('models', event.target.value)} />
            </Field>
            <Field label="Allowed connectors" hint="Empty = unconstrained.">
              <input className={INPUT} value={form.connectors} onChange={(event) => update('connectors', event.target.value)} placeholder="jira, slack" />
            </Field>
            <Field label="Allowed Regions" hint="Empty = unconstrained.">
              <input className={INPUT} value={form.regions} onChange={(event) => update('regions', event.target.value)} placeholder="us-east-1" />
            </Field>
          </div>

          <ChoiceGroup
            label="Allowed tool risk classes"
            hint="Empty = unconstrained. Constrained, an unclassified tool is refused, not floored."
            options={ALLOWED_TOOL_RISK_CLASSES}
            selected={form.toolRiskClasses}
            onChange={(next) => update('toolRiskClasses', next)}
          />
          <ChoiceGroup
            label="Allowed data classes"
            hint="Empty = unconstrained."
            options={ALLOWED_DATA_CLASSES}
            selected={form.dataClasses}
            onChange={(next) => update('dataClasses', next)}
          />
          <ChoiceGroup
            label="Allowed channels"
            hint="Empty = unconstrained."
            options={CHANNEL_TYPES}
            selected={form.channels}
            onChange={(next) => update('channels', next)}
          />

          <div className="grid grid-cols-3 gap-2">
            <Field label="Per-agent monthly budget (USD)" hint="Default for an agent that declares none.">
              <input className={INPUT} inputMode="decimal" value={form.perAgentMonthlyUsd} onChange={(event) => update('perAgentMonthlyUsd', event.target.value)} />
            </Field>
            <Field label="Per-agent cap (USD)" hint="Admission refuses an agent above it.">
              <input className={INPUT} inputMode="decimal" value={form.maxAgentMonthlyUsd} onChange={(event) => update('maxAgentMonthlyUsd', event.target.value)} />
            </Field>
            <Field label="Workspace monthly budget (USD)" hint="Recorded for chargeback; not enforced at admission.">
              <input className={INPUT} inputMode="decimal" value={form.perWorkspaceMonthlyUsd} onChange={(event) => update('perWorkspaceMonthlyUsd', event.target.value)} />
            </Field>
          </div>

          <div className="grid grid-cols-3 gap-2">
            <Field label="Approvers: low risk" hint="Empty = auto-approve.">
              <input className={INPUT} value={form.approvalLow} onChange={(event) => update('approvalLow', event.target.value)} />
            </Field>
            <Field label="Approvers: medium risk" hint="Every group must approve.">
              <input className={INPUT} value={form.approvalMedium} onChange={(event) => setRouting('approvalMedium', event.target.value)} />
            </Field>
            <Field label="Approvers: high risk" hint="Every group must approve.">
              <input className={INPUT} value={form.approvalHigh} onChange={(event) => setRouting('approvalHigh', event.target.value)} />
            </Field>
          </div>
          <label className="flex items-center gap-2 text-[11px]">
            <input
              type="checkbox"
              checked={form.reducedReviewAcknowledged}
              onChange={(event) => update('reducedReviewAcknowledged', event.target.checked)}
            />
            {REDUCED_REVIEW_ACKNOWLEDGEMENT}
          </label>

          <div className="grid grid-cols-3 gap-2">
            <Field label="Platform version default">
              <select
                className={INPUT}
                value={form.platformVersion}
                onChange={(event) => update('platformVersion', event.target.value as '' | PlatformVersion)}
              >
                <option value="">platform default</option>
                <option value="V1">V1</option>
                <option value="V2">V2</option>
              </select>
            </Field>
            <Field label="Gateway guardrail profile">
              <select
                className={INPUT}
                value={form.guardrailProfile}
                onChange={(event) => update('guardrailProfile', event.target.value as GuardrailProfile)}
              >
                {GUARDRAIL_PROFILES.map((profile) => (
                  <option key={profile} value={profile}>
                    {profile}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Unclassified items floor the tier to">
              <select
                className={INPUT}
                value={form.unclassifiedRiskTier}
                onChange={(event) => update('unclassifiedRiskTier', event.target.value as RiskTier)}
              >
                {RISK_TIERS.map((tier) => (
                  <option key={tier} value={tier}>
                    {tier}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Memory default">
              <input className={INPUT} value={form.memory} onChange={(event) => update('memory', event.target.value)} />
            </Field>
            <label className="flex items-center gap-2 text-[11px] col-span-2 mt-4">
              <input
                type="checkbox"
                checked={form.trustBuilderClassifications}
                onChange={(event) => update('trustBuilderClassifications', event.target.checked)}
              />
              Trust builders' own risk and data classifications (developer workspaces only)
            </label>
          </div>

          <div className="flex gap-2">
            <button type="submit" className={PRIMARY} disabled={busy}>
              {busy ? 'Saving…' : editingExisting ? 'Save changes' : 'Create workspace'}
            </button>
            <button type="button" className={BUTTON} onClick={() => setForm(null)} disabled={busy}>
              Cancel
            </button>
          </div>
        </form>
      )}
    </section>
  );
}
