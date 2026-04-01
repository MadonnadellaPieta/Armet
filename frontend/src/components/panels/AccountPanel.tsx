import { Card, Title, Text, Badge, Metric } from "@tremor/react";
import { useAccount } from "../../hooks/useStrategies";

function formatUSD(n: number) {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
  }).format(n);
}

export default function AccountPanel() {
  const { data: account, isLoading, error } = useAccount();

  return (
    <Card className="bg-gray-900 border-gray-800">
      <Title className="text-white mb-4">Account</Title>

      {isLoading && <Text className="text-gray-400">Loading account…</Text>}
      {error && (
        <Text className="text-red-400">
          Failed to load account: {error.message}
        </Text>
      )}
      {!isLoading && !error && !account && (
        <Text className="text-gray-500 italic">No account data available.</Text>
      )}

      {account && (
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <Text className="text-gray-400">Phase</Text>
            <Badge color={account.phase === "EVAL" ? "yellow" : "blue"} size="sm">
              {account.phase}
            </Badge>
          </div>

          <div>
            <Text className="text-gray-400 text-xs uppercase tracking-wider mb-1">
              Balance
            </Text>
            <Metric className="text-white">{formatUSD(account.balance)}</Metric>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <Text className="text-gray-400 text-xs">Equity</Text>
              <Text className="text-gray-200 font-mono text-sm">
                {formatUSD(account.equity)}
              </Text>
            </div>
            <div>
              <Text className="text-gray-400 text-xs">Daily P&L</Text>
              <Text
                className={`font-mono text-sm ${
                  account.daily_pnl >= 0 ? "text-green-400" : "text-red-400"
                }`}
              >
                {account.daily_pnl >= 0 ? "+" : ""}
                {formatUSD(account.daily_pnl)}
              </Text>
            </div>
          </div>

          <div>
            <Text className="text-gray-400 text-xs">EOD Drawdown Threshold</Text>
            <Text className="text-yellow-400 font-mono text-sm">
              {formatUSD(account.eod_threshold)}
            </Text>
          </div>
        </div>
      )}
    </Card>
  );
}
