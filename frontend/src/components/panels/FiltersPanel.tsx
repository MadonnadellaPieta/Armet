import { Card, Title, Text, Badge } from "@tremor/react";
import { useStrategies } from "../../hooks/useStrategies";
import type { FilterInfo } from "../../api/client";

const FALLBACK_FILTERS: FilterInfo[] = [
  { name: "order_flow", enabled: true, weight: 0.25 },
  { name: "market_profile", enabled: true, weight: 0.25 },
  { name: "support_resistance", enabled: true, weight: 0.25 },
];

const FILTER_LABELS: Record<string, string> = {
  order_flow: "Order Flow",
  market_profile: "Market Profile",
  support_resistance: "Support & Resistance",
};

export default function FiltersPanel() {
  const { data, isLoading } = useStrategies();

  const filters =
    data?.filters && data.filters.length > 0
      ? data.filters
      : FALLBACK_FILTERS;

  return (
    <Card className="bg-gray-900 border-gray-800">
      <Title className="text-white mb-4">Confluence Filters</Title>

      {isLoading && <Text className="text-gray-400">Loading filters…</Text>}

      {!isLoading && (
        <div className="space-y-3">
          {filters.map((filter) => (
            <div
              key={filter.name}
              className="rounded-lg bg-gray-800 border border-gray-700 p-3"
            >
              <div className="flex items-center justify-between mb-2">
                <Text className="text-gray-200 font-semibold text-sm">
                  {FILTER_LABELS[filter.name] ?? filter.name}
                </Text>
                <Badge color={filter.enabled ? "green" : "gray"} size="sm">
                  {filter.enabled ? "ON" : "OFF"}
                </Badge>
              </div>
              <div className="flex justify-between text-xs">
                <Text className="text-gray-500">Weight</Text>
                <Text className="text-gray-300 font-mono">
                  {filter.weight.toFixed(2)}
                </Text>
              </div>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}
