import { describe, it, expect, vi, beforeEach } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { renderWithQuery } from "./helpers";
import CircuitBreakerPanel from "../components/panels/CircuitBreakerPanel";
import type { CircuitBreakerStatus } from "../api/client";

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

const notTripped: CircuitBreakerStatus = {
  is_tripped: false,
  trigger: null,
  tripped_at: null,
  reason: null,
};

const tripped: CircuitBreakerStatus = {
  is_tripped: true,
  trigger: "MAX_DAILY_LOSS",
  tripped_at: "2026-04-01T14:30:00Z",
  reason: "Daily loss limit exceeded",
};

describe("CircuitBreakerPanel", () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it("shows green OK badge when not tripped", async () => {
    vi.mocked(client.get).mockResolvedValue({ data: notTripped });
    renderWithQuery(<CircuitBreakerPanel />);

    await waitFor(() => {
      expect(screen.getByText("OK")).toBeInTheDocument();
    });
  });

  it("shows red TRIPPED badge when tripped", async () => {
    vi.mocked(client.get).mockResolvedValue({ data: tripped });
    renderWithQuery(<CircuitBreakerPanel />);

    await waitFor(() => {
      expect(screen.getByText("TRIPPED")).toBeInTheDocument();
    });

    expect(screen.getByText("MAX_DAILY_LOSS")).toBeInTheDocument();
    expect(screen.getByText("Daily loss limit exceeded")).toBeInTheDocument();
  });

  it("calls /circuit-breaker endpoint", async () => {
    vi.mocked(client.get).mockResolvedValue({ data: notTripped });
    renderWithQuery(<CircuitBreakerPanel />);

    await waitFor(() => {
      expect(client.get).toHaveBeenCalledWith("/circuit-breaker");
    });
  });
});
