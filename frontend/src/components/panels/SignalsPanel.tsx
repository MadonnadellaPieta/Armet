import { useState, useEffect, useCallback } from "react";
import {
  Card,
  Title,
  Badge,
  Table,
  TableHead,
  TableHeaderCell,
  TableBody,
  TableRow,
  TableCell,
  Text,
} from "@tremor/react";
import { useSignals } from "../../hooks/useSignals";
import type { Signal } from "../../api/client";

function confidenceColor(score: number): "green" | "yellow" | "red" {
  if (score >= 0.7) return "green";
  if (score >= 0.4) return "yellow";
  return "red";
}

function decisionColor(
  decision: Signal["decision"],
): "green" | "red" | "gray" | "orange" {
  if (decision === "ACCEPTED") return "green";
  if (decision === "REJECTED") return "red";
  if (decision === "EXPIRED") return "orange";
  return "gray";
}

function formatPrice(p: number) {
  return p.toFixed(2);
}

function formatTimestamp(ts: string) {
  return new Date(ts).toLocaleTimeString();
}

function CountdownButton({ signal }: { signal: Signal }) {
  const [remaining, setRemaining] = useState<number>(() => {
    const elapsed = (Date.now() - new Date(signal.timestamp).getTime()) / 1000;
    return Math.max(0, signal.expiry_seconds - elapsed);
  });

  useEffect(() => {
    if (remaining <= 0) return;
    const interval = setInterval(() => {
      const elapsed =
        (Date.now() - new Date(signal.timestamp).getTime()) / 1000;
      const left = Math.max(0, signal.expiry_seconds - elapsed);
      setRemaining(left);
      if (left <= 0) clearInterval(interval);
    }, 250);
    return () => clearInterval(interval);
  }, [signal.timestamp, signal.expiry_seconds, remaining]);

  const handleAccept = useCallback(() => {
    console.log("ACCEPT signal", signal.id, signal);
  }, [signal]);

  const handleReject = useCallback(() => {
    console.log("REJECT signal", signal.id, signal);
  }, [signal]);

  if (remaining <= 0) {
    return <Badge color="orange">EXPIRED</Badge>;
  }

  return (
    <div className="flex items-center gap-2">
      <span className="text-xs font-mono text-yellow-400 w-8">
        {Math.ceil(remaining)}s
      </span>
      <button
        onClick={handleAccept}
        className="px-2 py-1 text-xs font-semibold rounded bg-green-700 hover:bg-green-600 text-white transition-colors"
      >
        ACCEPT
      </button>
      <button
        onClick={handleReject}
        className="px-2 py-1 text-xs font-semibold rounded bg-red-700 hover:bg-red-600 text-white transition-colors"
      >
        REJECT
      </button>
    </div>
  );
}

export default function SignalsPanel() {
  const { data: signals, isLoading, error } = useSignals(20);

  // Find newest pending signal (decision === null)
  const pendingSignal = signals?.find((s) => s.decision === null) ?? null;

  return (
    <Card className="bg-gray-900 border-gray-800">
      <Title className="text-white mb-4">Live Signals</Title>

      {isLoading && (
        <Text className="text-gray-400">Loading signals…</Text>
      )}
      {error && (
        <Text className="text-red-400">Failed to load signals: {error.message}</Text>
      )}
      {!isLoading && !error && (!signals || signals.length === 0) && (
        <Text className="text-gray-500 italic">No signals yet.</Text>
      )}

      {signals && signals.length > 0 && (
        <Table>
          <TableHead>
            <TableRow>
              <TableHeaderCell className="text-gray-400">Time</TableHeaderCell>
              <TableHeaderCell className="text-gray-400">Instrument</TableHeaderCell>
              <TableHeaderCell className="text-gray-400">Strategy</TableHeaderCell>
              <TableHeaderCell className="text-gray-400">Dir</TableHeaderCell>
              <TableHeaderCell className="text-gray-400">Entry</TableHeaderCell>
              <TableHeaderCell className="text-gray-400">SL</TableHeaderCell>
              <TableHeaderCell className="text-gray-400">TP</TableHeaderCell>
              <TableHeaderCell className="text-gray-400">R:R</TableHeaderCell>
              <TableHeaderCell className="text-gray-400">Conf</TableHeaderCell>
              <TableHeaderCell className="text-gray-400">Decision</TableHeaderCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {signals.map((signal) => (
              <TableRow key={signal.id} className="border-gray-800">
                <TableCell className="text-gray-300 font-mono text-xs">
                  {formatTimestamp(signal.timestamp)}
                </TableCell>
                <TableCell className="text-gray-200 font-semibold">
                  {signal.instrument}
                </TableCell>
                <TableCell className="text-gray-300 text-xs">
                  {signal.strategy_name}
                </TableCell>
                <TableCell>
                  <Badge
                    color={signal.direction === "LONG" ? "green" : "red"}
                    size="sm"
                  >
                    {signal.direction}
                  </Badge>
                </TableCell>
                <TableCell className="text-gray-200 font-mono">
                  {formatPrice(signal.entry_price)}
                </TableCell>
                <TableCell className="text-red-400 font-mono">
                  {formatPrice(signal.stop_loss_price)}
                </TableCell>
                <TableCell className="text-green-400 font-mono">
                  {formatPrice(signal.take_profit_price)}
                </TableCell>
                <TableCell className="text-gray-300 font-mono">
                  {signal.rr_ratio.toFixed(2)}
                </TableCell>
                <TableCell>
                  <Badge
                    color={confidenceColor(signal.confidence_score)}
                    size="sm"
                  >
                    {(signal.confidence_score * 100).toFixed(0)}%
                  </Badge>
                </TableCell>
                <TableCell>
                  {signal.decision === null && signal.id === pendingSignal?.id ? (
                    <CountdownButton signal={signal} />
                  ) : (
                    <Badge
                      color={decisionColor(signal.decision)}
                      size="sm"
                    >
                      {signal.decision ?? "PENDING"}
                    </Badge>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </Card>
  );
}
