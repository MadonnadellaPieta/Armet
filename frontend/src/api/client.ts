import axios from "axios";

const client = axios.create({
  baseURL: "/api/v1",
  headers: {
    "Content-Type": "application/json",
  },
});

export default client;

// ─── API types ────────────────────────────────────────────────────────────────

export type Direction = "LONG" | "SHORT";
export type SignalDecision = "ACCEPTED" | "REJECTED" | "EXPIRED" | null;

export interface Signal {
  id: string;
  instrument: string;
  strategy_name: string;
  direction: Direction;
  entry_price: number;
  stop_loss_price: number;
  take_profit_price: number;
  rr_ratio: number;
  confidence_score: number;
  confluence_factors: Record<string, unknown>;
  indicator_state: Record<string, unknown>;
  timestamp: string;
  expiry_seconds: number;
  decision: SignalDecision;
  decision_timestamp: string | null;
}

export interface Position {
  instrument: string;
  quantity: number;
  avg_entry_price: number;
  unrealized_pnl: number;
  side: Direction;
}

export interface AccountInfo {
  balance: number;
  equity: number;
  daily_pnl: number;
  eod_threshold: number;
  phase: string;
}

export interface StrategyParams {
  [key: string]: unknown;
}

export interface StrategyInfo {
  name: string;
  enabled: boolean;
  params: StrategyParams;
}

export interface FilterInfo {
  name: string;
  enabled: boolean;
  weight: number;
}

export interface StrategiesResponse {
  strategies: StrategyInfo[];
  filters: FilterInfo[];
}

export interface CircuitBreakerStatus {
  is_tripped: boolean;
  trigger: string | null;
  tripped_at: string | null;
  reason: string | null;
}
