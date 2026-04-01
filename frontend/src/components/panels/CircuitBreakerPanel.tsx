import { Card, Title, Text, Badge } from "@tremor/react";
import {
  useCircuitBreaker,
  useTripCircuitBreaker,
  useResetCircuitBreaker,
} from "../../hooks/useCircuitBreaker";

function formatTimestamp(ts: string | null) {
  if (!ts) return "—";
  return new Date(ts).toLocaleString();
}

export default function CircuitBreakerPanel() {
  const { data: cb, isLoading, error } = useCircuitBreaker();
  const trip = useTripCircuitBreaker();
  const reset = useResetCircuitBreaker();

  function handleTrip() {
    const reason = window.prompt("Enter reason for tripping circuit breaker:");
    if (reason === null) return;
    trip.mutate({ reason: reason.trim() || "Manual trip" });
  }

  function handleReset() {
    reset.mutate();
  }

  return (
    <Card className="bg-gray-900 border-gray-800">
      <Title className="text-white mb-4">Circuit Breaker</Title>

      {isLoading && <Text className="text-gray-400">Loading…</Text>}
      {error && (
        <Text className="text-red-400">
          Failed to load: {error.message}
        </Text>
      )}

      {cb && (
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <Text className="text-gray-400">Status</Text>
            <Badge color={cb.is_tripped ? "red" : "green"} size="lg">
              {cb.is_tripped ? "TRIPPED" : "OK"}
            </Badge>
          </div>

          {cb.trigger && (
            <div className="flex items-center justify-between">
              <Text className="text-gray-400">Trigger</Text>
              <Text className="text-gray-200 font-mono text-sm">
                {cb.trigger}
              </Text>
            </div>
          )}

          {cb.reason && (
            <div>
              <Text className="text-gray-400 text-xs">Reason</Text>
              <Text className="text-gray-300 text-sm">{cb.reason}</Text>
            </div>
          )}

          {cb.tripped_at && (
            <div>
              <Text className="text-gray-400 text-xs">Tripped At</Text>
              <Text className="text-gray-300 text-sm font-mono">
                {formatTimestamp(cb.tripped_at)}
              </Text>
            </div>
          )}

          <div className="flex gap-2 pt-2">
            <button
              onClick={handleTrip}
              disabled={cb.is_tripped || trip.isPending}
              className="flex-1 px-3 py-2 text-sm font-semibold rounded bg-red-700 hover:bg-red-600 disabled:opacity-40 disabled:cursor-not-allowed text-white transition-colors"
            >
              {trip.isPending ? "Tripping…" : "Trip"}
            </button>
            <button
              onClick={handleReset}
              disabled={!cb.is_tripped || reset.isPending}
              className="flex-1 px-3 py-2 text-sm font-semibold rounded bg-blue-700 hover:bg-blue-600 disabled:opacity-40 disabled:cursor-not-allowed text-white transition-colors"
            >
              {reset.isPending ? "Resetting…" : "Reset"}
            </button>
          </div>
        </div>
      )}
    </Card>
  );
}
