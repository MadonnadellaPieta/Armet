import { useQuery } from "@tanstack/react-query";
import client, { type Signal } from "../api/client";

async function fetchSignals(limit = 20): Promise<Signal[]> {
  const { data } = await client.get<Signal[]>("/signals", {
    params: { limit },
  });
  return data;
}

export function useSignals(limit = 20) {
  return useQuery<Signal[], Error>({
    queryKey: ["signals", limit],
    queryFn: () => fetchSignals(limit),
    refetchInterval: 2_000,
  });
}
