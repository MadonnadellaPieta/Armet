import { Card, Title, Text, Badge, Switch } from "@tremor/react";
import { useStrategies, useToggleStrategy } from "../../hooks/useStrategies";

export default function StrategiesPanel() {
  const { data, isLoading, error } = useStrategies();
  const toggle = useToggleStrategy();

  return (
    <Card className="bg-gray-900 border-gray-800">
      <Title className="text-white mb-4">Strategies</Title>

      {isLoading && <Text className="text-gray-400">Loading strategies…</Text>}
      {error && (
        <Text className="text-red-400">
          Failed to load strategies: {error.message}
        </Text>
      )}

      {data && (
        <div className="space-y-4">
          {data.strategies.map((strategy) => (
            <div
              key={strategy.name}
              className="rounded-lg bg-gray-800 border border-gray-700 p-3"
            >
              <div className="flex items-center justify-between mb-2">
                <div className="flex items-center gap-2">
                  <Badge
                    color={strategy.enabled ? "green" : "gray"}
                    size="sm"
                  >
                    {strategy.enabled ? "ON" : "OFF"}
                  </Badge>
                  <Text className="text-gray-200 font-semibold text-sm">
                    {strategy.name}
                  </Text>
                </div>
                <Switch
                  id={`strategy-${strategy.name}`}
                  checked={strategy.enabled}
                  onChange={(checked) =>
                    toggle.mutate({ name: strategy.name, enable: checked })
                  }
                />
              </div>
              <div className="space-y-1">
                {Object.entries(strategy.params).map(([k, v]) => (
                  <div key={k} className="flex justify-between text-xs">
                    <Text className="text-gray-500">{k}</Text>
                    <Text className="text-gray-300 font-mono">
                      {String(v)}
                    </Text>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}
