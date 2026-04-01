import {
  Card,
  Title,
  Table,
  TableHead,
  TableHeaderCell,
  TableBody,
  TableRow,
  TableCell,
  Text,
  Badge,
} from "@tremor/react";
import { usePositions } from "../../hooks/useStrategies";

export default function PositionsPanel() {
  const { data: positions, isLoading, error } = usePositions();

  return (
    <Card className="bg-gray-900 border-gray-800">
      <Title className="text-white mb-4">Open Positions</Title>

      {isLoading && <Text className="text-gray-400">Loading positions…</Text>}
      {error && (
        <Text className="text-red-400">
          Failed to load positions: {error.message}
        </Text>
      )}
      {!isLoading && !error && (!positions || positions.length === 0) && (
        <Text className="text-gray-500 italic">No open positions.</Text>
      )}

      {positions && positions.length > 0 && (
        <Table>
          <TableHead>
            <TableRow>
              <TableHeaderCell className="text-gray-400">Instrument</TableHeaderCell>
              <TableHeaderCell className="text-gray-400">Side</TableHeaderCell>
              <TableHeaderCell className="text-gray-400">Qty</TableHeaderCell>
              <TableHeaderCell className="text-gray-400">Avg Entry</TableHeaderCell>
              <TableHeaderCell className="text-gray-400">Unrealized P&L</TableHeaderCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {positions.map((pos) => (
              <TableRow key={`${pos.instrument}-${pos.side}`} className="border-gray-800">
                <TableCell className="text-gray-200 font-semibold">
                  {pos.instrument}
                </TableCell>
                <TableCell>
                  <Badge color={pos.side === "LONG" ? "green" : "red"} size="sm">
                    {pos.side}
                  </Badge>
                </TableCell>
                <TableCell className="text-gray-300">{pos.quantity}</TableCell>
                <TableCell className="text-gray-200 font-mono">
                  {pos.avg_entry_price.toFixed(2)}
                </TableCell>
                <TableCell
                  className={
                    pos.unrealized_pnl >= 0
                      ? "text-green-400 font-mono"
                      : "text-red-400 font-mono"
                  }
                >
                  {pos.unrealized_pnl >= 0 ? "+" : ""}
                  {pos.unrealized_pnl.toFixed(2)}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </Card>
  );
}
