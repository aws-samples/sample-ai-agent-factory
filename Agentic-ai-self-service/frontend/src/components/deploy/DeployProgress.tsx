/**
 * DeployProgress - displays deployment progress indicator.
 */

interface DeployProgressProps {
  message: string;
}

export function DeployProgress({ message }: DeployProgressProps) {
  return (
    <div className="flex items-center gap-3 p-3.5 bg-[#ff9900]/5 rounded-lg border border-[#ff9900]/20">
      <div className="w-5 h-5 border-2 border-[#d45b07] border-t-transparent rounded-full animate-spin flex-shrink-0" />
      <span className="text-[#16191f] text-sm font-medium">{message}</span>
    </div>
  );
}
