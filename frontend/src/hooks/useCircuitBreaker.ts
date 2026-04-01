import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import client, { type CircuitBreakerStatus } from "../api/client";

async function fetchCircuitBreaker(): Promise<CircuitBreakerStatus> {
  const { data } = await client.get<CircuitBreakerStatus>("/circuit-breaker");
  return data;
}

export function useCircuitBreaker() {
  return useQuery<CircuitBreakerStatus, Error>({
    queryKey: ["circuit-breaker"],
    queryFn: fetchCircuitBreaker,
    refetchInterval: 3_000,
  });
}

export function useTripCircuitBreaker() {
  const qc = useQueryClient();
  return useMutation<void, Error, { reason: string }>({
    mutationFn: async ({ reason }) => {
      await client.post("/circuit-breaker/trip", { reason });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["circuit-breaker"] });
    },
  });
}

export function useResetCircuitBreaker() {
  const qc = useQueryClient();
  return useMutation<void, Error, void>({
    mutationFn: async () => {
      await client.post("/circuit-breaker/reset");
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["circuit-breaker"] });
    },
  });
}
