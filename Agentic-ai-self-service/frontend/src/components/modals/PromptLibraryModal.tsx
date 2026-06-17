/**
 * PromptLibraryModal — Phase 3 Gap 3H.
 *
 * Browse / create / edit reusable library prompts and manage their versions.
 *
 * Two modes:
 *   - management mode (default): full CRUD + version management, opened from a
 *     toolbar/palette control.
 *   - picker mode (`mode="picker"`): the same list, but selecting a prompt
 *     resolves it via the API and calls `onSelect({ promptName, versionId,
 *     body })`. Used by the RuntimeConfigurationModal "Use from library"
 *     control to write the resolved body into the System Prompt field.
 *
 * Uses the api.ts helpers added in the Gap 3H "Prompt Library" section:
 * listPromptsApi / createPromptApi / addPromptVersionApi /
 * promotePromptVersionApi / resolvePromptApi / deletePromptApi.
 */

import { useCallback, useEffect, useState } from 'react';
import {
  listPromptsApi,
  createPromptApi,
  addPromptVersionApi,
  promotePromptVersionApi,
  resolvePromptApi,
  deletePromptApi,
  type PromptEntry,
} from '../../services/api';

export interface PromptSelection {
  promptName: string;
  versionId: string;
  body: string;
}

export interface PromptLibraryModalProps {
  isOpen: boolean;
  onClose: () => void;
  /** "management" = full CRUD; "picker" = select-and-resolve for a config. */
  mode?: 'management' | 'picker';
  /** Called in picker mode when the user selects a prompt version. */
  onSelect?: (selection: PromptSelection) => void;
}

export function PromptLibraryModal({
  isOpen,
  onClose,
  mode = 'management',
  onSelect,
}: PromptLibraryModalProps) {
  const [prompts, setPrompts] = useState<PromptEntry[]>([]);
  const [scope, setScope] = useState<'all' | 'mine'>('all');
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);

  // Create form
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState('');
  const [newDescription, setNewDescription] = useState('');
  const [newTags, setNewTags] = useState('');
  const [newBody, setNewBody] = useState('');

  // Add-version form (keyed by prompt_name)
  const [addVersionFor, setAddVersionFor] = useState<string | null>(null);
  const [versionBody, setVersionBody] = useState('');

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const items = await listPromptsApi({ q: query || undefined, scope });
      setPrompts(items);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load prompts');
    } finally {
      setLoading(false);
    }
  }, [query, scope]);

  useEffect(() => {
    if (isOpen) {
      void refresh();
    }
  }, [isOpen, refresh]);

  const handleCreate = useCallback(async () => {
    if (!newName.trim() || !newBody.trim()) {
      setError('Name and prompt body are required.');
      return;
    }
    setError(null);
    try {
      await createPromptApi({
        display_name: newName.trim(),
        description: newDescription.trim(),
        tags: newTags
          .split(',')
          .map((t) => t.trim())
          .filter(Boolean),
        body: newBody,
      });
      setShowCreate(false);
      setNewName('');
      setNewDescription('');
      setNewTags('');
      setNewBody('');
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to create prompt');
    }
  }, [newName, newDescription, newTags, newBody, refresh]);

  const handleAddVersion = useCallback(
    async (promptName: string) => {
      if (!versionBody.trim()) {
        setError('Version body is required.');
        return;
      }
      setError(null);
      try {
        await addPromptVersionApi(promptName, { body: versionBody });
        setAddVersionFor(null);
        setVersionBody('');
        await refresh();
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Failed to add version');
      }
    },
    [versionBody, refresh],
  );

  const handlePromote = useCallback(
    async (promptName: string, versionId: string) => {
      setError(null);
      try {
        await promotePromptVersionApi(promptName, versionId);
        await refresh();
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Failed to promote version');
      }
    },
    [refresh],
  );

  const handleDelete = useCallback(
    async (promptName: string) => {
      setError(null);
      try {
        await deletePromptApi(promptName);
        await refresh();
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Failed to delete prompt');
      }
    },
    [refresh],
  );

  const handlePick = useCallback(
    async (promptName: string, versionId?: string) => {
      setError(null);
      try {
        const resolved = await resolvePromptApi(promptName, versionId);
        onSelect?.({
          promptName: resolved.prompt_name,
          versionId: resolved.version_id,
          body: resolved.body,
        });
        onClose();
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Failed to resolve prompt');
      }
    },
    [onSelect, onClose],
  );

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-lg shadow-xl w-full max-w-3xl max-h-[85vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b">
          <h2 className="text-lg font-semibold">
            {mode === 'picker' ? 'Select a Library Prompt' : 'Prompt Library'}
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="text-gray-400 hover:text-gray-600"
            aria-label="Close"
          >
            ✕
          </button>
        </div>

        {/* Controls */}
        <div className="flex items-center gap-2 px-6 py-3 border-b">
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && void refresh()}
            placeholder="Search prompts..."
            className="flex-1 px-3 py-1.5 text-sm border rounded"
          />
          <select
            value={scope}
            onChange={(e) => setScope(e.target.value as 'all' | 'mine')}
            className="px-2 py-1.5 text-sm border rounded"
          >
            <option value="all">All</option>
            <option value="mine">Mine</option>
          </select>
          <button
            type="button"
            onClick={() => void refresh()}
            className="px-3 py-1.5 text-sm border rounded hover:bg-gray-50"
          >
            Search
          </button>
          {mode === 'management' && (
            <button
              type="button"
              onClick={() => setShowCreate((v) => !v)}
              className="px-3 py-1.5 text-sm text-white bg-blue-600 rounded hover:bg-blue-700"
            >
              {showCreate ? 'Cancel' : 'New Prompt'}
            </button>
          )}
        </div>

        {error && (
          <div className="px-6 py-2 text-sm text-red-600 bg-red-50 border-b">
            {error}
          </div>
        )}

        {/* Create form */}
        {showCreate && mode === 'management' && (
          <div className="px-6 py-4 border-b bg-gray-50 space-y-2">
            <input
              type="text"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="Prompt name"
              className="w-full px-3 py-1.5 text-sm border rounded"
            />
            <input
              type="text"
              value={newDescription}
              onChange={(e) => setNewDescription(e.target.value)}
              placeholder="Description (optional)"
              className="w-full px-3 py-1.5 text-sm border rounded"
            />
            <input
              type="text"
              value={newTags}
              onChange={(e) => setNewTags(e.target.value)}
              placeholder="Tags (comma-separated)"
              className="w-full px-3 py-1.5 text-sm border rounded"
            />
            <textarea
              value={newBody}
              onChange={(e) => setNewBody(e.target.value)}
              placeholder="Prompt body (the system prompt text)..."
              rows={6}
              className="w-full px-3 py-2 text-sm border rounded font-mono"
            />
            <button
              type="button"
              onClick={() => void handleCreate()}
              className="px-3 py-1.5 text-sm text-white bg-blue-600 rounded hover:bg-blue-700"
            >
              Create Prompt
            </button>
          </div>
        )}

        {/* List */}
        <div className="flex-1 overflow-y-auto px-6 py-3">
          {loading && <p className="text-sm text-gray-500">Loading...</p>}
          {!loading && prompts.length === 0 && (
            <p className="text-sm text-gray-500">No prompts found.</p>
          )}
          {prompts.map((p) => {
            const isExpanded = expanded === p.prompt_name;
            return (
              <div key={p.prompt_name} className="mb-3 border rounded">
                <div className="flex items-center justify-between px-4 py-3">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="font-medium truncate">{p.display_name}</span>
                      {!p.is_owner && (
                        <span className="px-1.5 py-0.5 text-xs text-gray-600 bg-gray-100 rounded">
                          shared
                        </span>
                      )}
                    </div>
                    {p.description && (
                      <p className="text-sm text-gray-500 truncate">{p.description}</p>
                    )}
                    <div className="flex gap-1 mt-1">
                      {p.tags.map((t) => (
                        <span
                          key={t}
                          className="px-1.5 py-0.5 text-xs text-blue-700 bg-blue-50 rounded"
                        >
                          {t}
                        </span>
                      ))}
                    </div>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    {mode === 'picker' && (
                      <button
                        type="button"
                        onClick={() => void handlePick(p.prompt_name)}
                        className="px-3 py-1 text-sm text-white bg-green-600 rounded hover:bg-green-700"
                      >
                        Use default
                      </button>
                    )}
                    <button
                      type="button"
                      onClick={() =>
                        setExpanded(isExpanded ? null : p.prompt_name)
                      }
                      className="px-3 py-1 text-sm border rounded hover:bg-gray-50"
                    >
                      {isExpanded ? 'Hide' : `Versions (${p.versions.length})`}
                    </button>
                    {mode === 'management' && p.is_owner && (
                      <button
                        type="button"
                        onClick={() => void handleDelete(p.prompt_name)}
                        className="px-3 py-1 text-sm text-red-600 border border-red-200 rounded hover:bg-red-50"
                      >
                        Delete
                      </button>
                    )}
                  </div>
                </div>

                {isExpanded && (
                  <div className="px-4 py-3 border-t bg-gray-50 space-y-2">
                    {p.versions.map((v) => {
                      const isDefault = v.version_id === p.default_version_id;
                      return (
                        <div
                          key={v.version_id}
                          className="flex items-start justify-between gap-3 p-2 bg-white border rounded"
                        >
                          <div className="min-w-0 flex-1">
                            <div className="flex items-center gap-2">
                              <code className="text-xs text-gray-500">
                                {v.version_id.slice(0, 12)}
                              </code>
                              {isDefault && (
                                <span className="px-1.5 py-0.5 text-xs text-green-700 bg-green-50 rounded">
                                  default
                                </span>
                              )}
                            </div>
                            <pre className="mt-1 text-xs text-gray-700 whitespace-pre-wrap break-words max-h-24 overflow-y-auto">
                              {v.body}
                            </pre>
                          </div>
                          <div className="flex flex-col gap-1 shrink-0">
                            {mode === 'picker' && (
                              <button
                                type="button"
                                onClick={() =>
                                  void handlePick(p.prompt_name, v.version_id)
                                }
                                className="px-2 py-1 text-xs text-white bg-green-600 rounded hover:bg-green-700"
                              >
                                Use
                              </button>
                            )}
                            {mode === 'management' &&
                              p.is_owner &&
                              !isDefault && (
                                <button
                                  type="button"
                                  onClick={() =>
                                    void handlePromote(
                                      p.prompt_name,
                                      v.version_id,
                                    )
                                  }
                                  className="px-2 py-1 text-xs border rounded hover:bg-gray-100"
                                >
                                  Set default
                                </button>
                              )}
                          </div>
                        </div>
                      );
                    })}

                    {mode === 'management' && p.is_owner && (
                      <div>
                        {addVersionFor === p.prompt_name ? (
                          <div className="space-y-2">
                            <textarea
                              value={versionBody}
                              onChange={(e) => setVersionBody(e.target.value)}
                              placeholder="New version body..."
                              rows={4}
                              className="w-full px-3 py-2 text-sm border rounded font-mono"
                            />
                            <div className="flex gap-2">
                              <button
                                type="button"
                                onClick={() =>
                                  void handleAddVersion(p.prompt_name)
                                }
                                className="px-3 py-1 text-sm text-white bg-blue-600 rounded hover:bg-blue-700"
                              >
                                Save version
                              </button>
                              <button
                                type="button"
                                onClick={() => {
                                  setAddVersionFor(null);
                                  setVersionBody('');
                                }}
                                className="px-3 py-1 text-sm border rounded hover:bg-gray-50"
                              >
                                Cancel
                              </button>
                            </div>
                          </div>
                        ) : (
                          <button
                            type="button"
                            onClick={() => {
                              setAddVersionFor(p.prompt_name);
                              setVersionBody('');
                            }}
                            className="px-3 py-1 text-sm border rounded hover:bg-gray-100"
                          >
                            + Add version
                          </button>
                        )}
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

export default PromptLibraryModal;
