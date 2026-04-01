import { useState } from "react";
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

const PAGE_SIZE = 20;

function decisionColor(decision: Signal["decision"]): "green" | "red" | "orange" {
  if (decision === "ACCEPTED") return "green";
  if (decision === "REJECTED") return "red";
  return "orange"; // EXPIRED
}

function formatTimestamp(ts: string) {
  return new Date(ts).toLocaleString();
}

export default function TradeHistoryPanel() {
  const [page, setPage] = useState(0);
  // Fetch more signals to paginate from
  const { data: signals, isLoading, error } = useSignals(200);

  const decided = (signals ?? []).filter((s) => s.decision !== null);
  const totalPages = Math.max(1, Math.ceil(decided.length / PAGE_SIZE));
  const pageSlice = decided.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);

  return (
    <Card className="bg-gray-900 border-gray-800">
      <div className="flex items-center justify-between mb-4">
        <Title className="text-white">Trade History</Title>
        <Text className="text-gray-500 text-sm">{decided.length} records</Text>
      </div>

      {isLoading && <Text className="text-gray-400">Loading history…</Text>}
      {error && (
        <Text className="text-red-400">
          Failed to load history: {error.message}
        </Text>
      )}
      {!isLoading && !error && decided.length === 0 && (
        <Text className="text-gray-500 italic">No decided signals yet.</Text>
      )}

      {decided.length > 0 && (
        <>
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
              {pageSlice.map((signal) => (
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
                    {signal.entry_price.toFixed(2)}
                  </TableCell>
                  <TableCell className="text-red-400 font-mono">
                    {signal.stop_loss_price.toFixed(2)}
                  </TableCell>
                  <TableCell className="text-green-400 font-mono">
                    {signal.take_profit_price.toFixed(2)}
                  </TableCell>
                  <TableCell className="text-gray-300 font-mono">
                    {signal.rr_ratio.toFixed(2)}
                  </TableCell>
                  <TableCell className="text-gray-300 font-mono text-xs">
                    {(signal.confidence_score * 100).toFixed(0)}%
                  </TableCell>
                  <TableCell>
                    <Badge color={decisionColor(signal.decision)} size="sm">
                      {signal.decision}
                    </Badge>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>

          {totalPages > 1 && (
            <div className="flex items-center justify-center gap-3 mt-4">
              <button
                onClick={() => setPage((p) => Math.max(0, p - 1))}
                disabled={page === 0}
                className="px-3 py-1 text-xs rounded bg-gray-700 hover:bg-gray-600 disabled:opacity-40 text-white transition-colors"
              >
                Previous
              </button>
              <Text className="text-gray-400 text-xs">
                Page {page + 1} / {totalPages}
              </Text>
              <button
                onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
                disabled={page >= totalPages - 1}
                className="px-3 py-1 text-xs rounded bg-gray-700 hover:bg-gray-600 disabled:opacity-40 text-white transition-colors"
              >
                Next
              </button>
            </div>
          )}
        </>
      )}
    </Card>
  );
}
