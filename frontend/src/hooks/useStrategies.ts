import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import client, {
  type StrategiesResponse,
  type AccountInfo,
  type Position,
} from "../api/client";

async function fetchStrategies(): Promise<StrategiesResponse> {
  const { data } = await client.get<StrategiesResponse>("/strategies");
  return data;
}

export function useStrategies() {
  return useQuery<StrategiesResponse, Error>({
    queryKey: ["strategies"],
    queryFn: fetchStrategies,
  });
}

export function useToggleStrategy() {
  const qc = useQueryClient();
  return useMutation<void, Error, { name: string; enable: boolean }>({
    mutationFn: async ({ name, enable }) => {
      const action = enable ? "enable" : "disable";
      await client.post(`/strategies/${name}/${action}`);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["strategies"] });
    },
  });
}

export function usePositions() {
  return useQuery<Position[], Error>({
    queryKey: ["positions"],
    queryFn: async () => {
      const { data } = await client.get<Position[]>("/positions");
      return data;
    },
  });
}

export function useAccount() {
  return useQuery<AccountInfo, Error>({
    queryKey: ["account"],
    queryFn: async () => {
      const { data } = await client.get<AccountInfo>("/account");
      return data;
    },
  });
}
