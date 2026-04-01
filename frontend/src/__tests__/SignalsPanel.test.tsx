import { describe, it, expect, vi, beforeEach } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { renderWithQuery } from "./helpers";
import SignalsPanel from "../components/panels/SignalsPanel";
import type { Signal } from "../api/client";

// Mock the axios client
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

const mockSignals: Signal[] = [
  {
    id: "abc123",
    instrument: "ES",
    strategy_name: "VWAPReversion",
    direction: "LONG",
    entry_price: 5200.25,
    stop_loss_price: 5196.0,
    take_profit_price: 5212.75,
    rr_ratio: 2.94,
    confidence_score: 0.72,
    confluence_factors: {},
    indicator_state: {},
    timestamp: new Date(Date.now() - 5000).toISOString(),
    expiry_seconds: 10,
    decision: "ACCEPTED",
    decision_timestamp: new Date().toISOString(),
  },
  {
    id: "def456",
    instrument: "NQ",
    strategy_name: "EMACrossover",
    direction: "SHORT",
    entry_price: 18200.0,
    stop_loss_price: 18230.0,
    take_profit_price: 18140.0,
    rr_ratio: 2.0,
    confidence_score: 0.45,
    confluence_factors: {},
    indicator_state: {},
    timestamp: new Date(Date.now() - 3000).toISOString(),
    expiry_seconds: 10,
    decision: "REJECTED",
    decision_timestamp: new Date().toISOString(),
  },
];

describe("SignalsPanel", () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it("renders empty state when no signals", async () => {
    vi.mocked(client.get).mockResolvedValue({ data: [] });
    renderWithQuery(<SignalsPanel />);

    await waitFor(() => {
      expect(screen.getByText("No signals yet.")).toBeInTheDocument();
    });
  });

  it("renders signal rows with correct fields", async () => {
    vi.mocked(client.get).mockResolvedValue({ data: mockSignals });
    renderWithQuery(<SignalsPanel />);

    await waitFor(() => {
      expect(screen.getByText("ES")).toBeInTheDocument();
      expect(screen.getByText("NQ")).toBeInTheDocument();
    });

    expect(screen.getByText("VWAPReversion")).toBeInTheDocument();
    expect(screen.getByText("EMACrossover")).toBeInTheDocument();
    expect(screen.getByText("LONG")).toBeInTheDocument();
    expect(screen.getByText("SHORT")).toBeInTheDocument();
    expect(screen.getByText("5200.25")).toBeInTheDocument();
    expect(screen.getByText("ACCEPTED")).toBeInTheDocument();
    expect(screen.getByText("REJECTED")).toBeInTheDocument();
    expect(screen.getByText("72%")).toBeInTheDocument();
    expect(screen.getByText("45%")).toBeInTheDocument();
  });

  it("calls /signals endpoint with correct params", async () => {
    vi.mocked(client.get).mockResolvedValue({ data: [] });
    renderWithQuery(<SignalsPanel />);

    await waitFor(() => {
      expect(client.get).toHaveBeenCalledWith("/signals", {
        params: { limit: 20 },
      });
    });
  });
});
