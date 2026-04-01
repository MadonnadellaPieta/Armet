import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, ReactNode } from "react";
import { useSignals } from "../hooks/useSignals";
import { useStrategies, usePositions, useAccount } from "../hooks/useStrategies";
import { useCircuitBreaker } from "../hooks/useCircuitBreaker";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    default: {
      get: vi.fn(),
      post: vi.fn(),
    },
  };
});

import client from "../api/client";

function makeWrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: qc }, children);
}

describe("useSignals", () => {
  beforeEach(() => vi.resetAllMocks());

  it("calls GET /signals with limit param", async () => {
    vi.mocked(client.get).mockResolvedValue({ data: [] });
    const { result } = renderHook(() => useSignals(20), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(client.get).toHaveBeenCalledWith("/signals", {
      params: { limit: 20 },
    });
  });
});

describe("useStrategies", () => {
  beforeEach(() => vi.resetAllMocks());

  it("calls GET /strategies", async () => {
    vi.mocked(client.get).mockResolvedValue({
      data: { strategies: [], filters: [] },
    });
    const { result } = renderHook(() => useStrategies(), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(client.get).toHaveBeenCalledWith("/strategies");
  });
});

describe("usePositions", () => {
  beforeEach(() => vi.resetAllMocks());

  it("calls GET /positions", async () => {
    vi.mocked(client.get).mockResolvedValue({ data: [] });
    const { result } = renderHook(() => usePositions(), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(client.get).toHaveBeenCalledWith("/positions");
  });
});

describe("useAccount", () => {
  beforeEach(() => vi.resetAllMocks());

  it("calls GET /account", async () => {
    vi.mocked(client.get).mockResolvedValue({
      data: {
        balance: 100000,
        equity: 100500,
        daily_pnl: 500,
        eod_threshold: 97000,
        phase: "EVAL",
      },
    });
    const { result } = renderHook(() => useAccount(), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(client.get).toHaveBeenCalledWith("/account");
  });
});

describe("useCircuitBreaker", () => {
  beforeEach(() => vi.resetAllMocks());

  it("calls GET /circuit-breaker", async () => {
    vi.mocked(client.get).mockResolvedValue({
      data: {
        is_tripped: false,
        trigger: null,
        tripped_at: null,
        reason: null,
      },
    });
    const { result } = renderHook(() => useCircuitBreaker(), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(client.get).toHaveBeenCalledWith("/circuit-breaker");
  });
});
