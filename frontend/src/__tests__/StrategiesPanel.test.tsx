import { describe, it, expect, vi, beforeEach } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { renderWithQuery } from "./helpers";
import StrategiesPanel from "../components/panels/StrategiesPanel";
import type { StrategiesResponse } from "../api/client";

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

const mockData: StrategiesResponse = {
  strategies: [
    {
      name: "VWAPReversion",
      enabled: true,
      params: { sd_multiplier: 2.0, sl_buffer: 1.0 },
    },
    {
      name: "EMACrossover",
      enabled: false,
      params: { fast: 9, slow: 21, volume_multiplier: 1.5 },
    },
    {
      name: "OpeningRangeBreakout",
      enabled: true,
      params: { range_duration: "15min" },
    },
  ],
  filters: [],
};

describe("StrategiesPanel", () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it("renders all strategy names from mock data", async () => {
    vi.mocked(client.get).mockResolvedValue({ data: mockData });
    renderWithQuery(<StrategiesPanel />);

    await waitFor(() => {
      expect(screen.getByText("VWAPReversion")).toBeInTheDocument();
      expect(screen.getByText("EMACrossover")).toBeInTheDocument();
      expect(screen.getByText("OpeningRangeBreakout")).toBeInTheDocument();
    });
  });

  it("shows enabled/disabled badges correctly", async () => {
    vi.mocked(client.get).mockResolvedValue({ data: mockData });
    renderWithQuery(<StrategiesPanel />);

    await waitFor(() => {
      const onBadges = screen.getAllByText("ON");
      const offBadges = screen.getAllByText("OFF");
      expect(onBadges).toHaveLength(2);
      expect(offBadges).toHaveLength(1);
    });
  });

  it("calls /strategies endpoint", async () => {
    vi.mocked(client.get).mockResolvedValue({ data: mockData });
    renderWithQuery(<StrategiesPanel />);

    await waitFor(() => {
      expect(client.get).toHaveBeenCalledWith("/strategies");
    });
  });
});
