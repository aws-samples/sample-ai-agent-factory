import type { PlatformObservabilityPolicyState } from '../../hooks/usePlatformObservabilityPolicy';

export function PlatformObservabilityPolicyNotice({
  state,
  onRetry,
}: {
  state: PlatformObservabilityPolicyState;
  onRetry: () => void;
}) {
  if (state.status === 'ready') return null;

  if (state.status === 'loading') {
    return (
      <div
        className="rounded-md border border-blue-200 bg-blue-50 p-3 text-sm text-blue-900"
        role="status"
      >
        <div className="font-medium">
          Checking platform observability policy…
        </div>
        <p className="mt-1 text-xs">
          Save is temporarily disabled until the admin-managed policy is
          verified.
        </p>
      </div>
    );
  }

  return (
    <div
      className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-900"
      role="alert"
    >
      <div className="font-medium">
        Platform observability policy unavailable
      </div>
      <p className="mt-1 text-xs">{state.message}</p>
      <p className="mt-1 text-xs">
        Save remains disabled because an unreadable admin policy cannot safely
        be treated as disabled.
      </p>
      <button
        type="button"
        className="mt-2 rounded border border-red-300 bg-white px-2.5 py-1 text-xs font-medium text-red-800 hover:bg-red-100"
        onClick={onRetry}
      >
        Retry policy check
      </button>
    </div>
  );
}
