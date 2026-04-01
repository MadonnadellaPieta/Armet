# CLAUDE.md — Futures Trading Signal System

## Project Overview
Semi-automated futures trading signal system with Python/FastAPI backend and React frontend.
Connects to Rithmic (async-rithmic) for real-time market data and order execution on ES/MES/NQ/MNQ futures.
Runs three configurable strategies that generate signals, displayed on a web dashboard with 10-second accept/reject window.
Accepted signals place bracket orders (entry + SL + TP) through Rithmic. Enforces Apex Trader Funding 100K EOD rules as non-overridable guardrails.

## Tech Stack
- **Backend**: Python 3.11+, FastAPI, SQLAlchemy 2.0 async, aiosqlite, Alembic
- **Frontend**: React 18, Vite 5, TypeScript, Tailwind CSS 3, Tremor 3, shadcn/ui, @tanstack/react-query v5, axios
- **Broker**: async-rithmic v1.5.9 (primary), Tradovate (fallback, not yet built)
- **Database**: SQLite via aiosqlite (v1)
- **Testing (backend)**: pytest with pytest-asyncio 0.23.8, `asyncio_mode = "auto"` in pyproject.toml
- **Testing (frontend)**: Vitest + @testing-library/react, jsdom environment

## Commands
- **Run all backend tests**: `python -m pytest backend/tests/ -v` (155 tests, all passing)
- **Run single test file**: `python -m pytest backend/tests/test_risk_manager.py -v`
- **Run migrations**: `cd /home/user/Armet && alembic upgrade head` (needs `data/` dir to exist)
- **Install backend deps**: `pip install -r requirements.txt`
- **Install frontend deps**: `cd frontend && npm install`
- **Run frontend dev server**: `cd frontend && npm run dev` (proxies /api → localhost:8000)
- **Run frontend tests**: `cd frontend && npm test`
- **Build frontend**: `cd frontend && npm run build`
- **Start FastAPI server**: `uvicorn backend.api.main:app --reload --port 8000`

## Branch
All development on: `claude/create-react-dashboard-3kVcX`

## Architecture

### Core Models (`backend/core/models.py`)
- **Enums**: Direction, OrderType, OrderStatus, ConnectionStatus, Phase (EVAL/PA), SignalDecision, ImpactLevel, CircuitBreakerTrigger
- **Market data**: Tick (frozen), OrderBook, OrderBookLevel, Bar (frozen)
- **Account**: Position, AccountInfo (has balance, equity, daily_pnl, eod_threshold)
- **Orders**: BracketOrderRequest, OrderUpdate, BracketOrderResult
- **Signal**: id, instrument, strategy_name, direction, entry_price, stop_loss_price, take_profit_price, rr_ratio, confidence_score (float 0-1), confluence_factors (dict), indicator_state (dict), timestamp, expiry_seconds=10, decision, decision_timestamp

### Broker Layer (`backend/core/`)
- **BrokerClient ABC** (`broker_client.py`): 15 abstract methods (connect, disconnect, subscribe_market_data, place_bracket_order, etc.). Callback types: TickCallback, OrderBookCallback, OrderUpdateCallback, ConnectionCallback.
- **RithmicClient** (`rithmic_client.py`): Wraps async_rithmic.RithmicClient. Maps instruments to CME. Translates events to our model types. Has `HAS_ASYNC_RITHMIC` flag for graceful import.
- **PaperBrokerClient** (`paper_broker_client.py`): Simulates fills with configurable slippage. SL/TP monitoring via `inject_tick()`. P&L calc: `ticks * tick_value * quantity`. ES defaults: tick_size=0.25, tick_value=12.50.

### Strategies (`backend/strategies/`)
- **BaseStrategy ABC** (`base_strategy.py`): `on_bar(Bar) → Signal|None`, `default_params() → dict`, optional `on_tick(Tick) → Signal|None`. Has `enabled` property.
- **VWAPReversionStrategy** (`vwap_reversion.py`): Incremental VWAP with volume-weighted SD bands. Rejection candle / delta shift methods. Default: sd_multiplier=2.0, sl_buffer=1.0.
- **EMACrossoverStrategy** (`ema_crossover.py`): EMA crossover + volume confirmation. SL from recent swing. TP via rr_multiple. Default: fast=9, slow=21, volume_multiplier=1.5.
- **OpeningRangeBreakoutStrategy** (`opening_range.py`): Builds range during first N minutes after 13:30 UTC. One breakout per direction per session. Default: range_duration=15min.

### Confluence Filters (`backend/filters/`)
All three have `evaluate(direction, price_level) → dict` returning `{aligned: bool, confidence_adjustment: float [-1,1], details: dict}`.
- **OrderFlowFilter** (`order_flow.py`): Trade classification (buy/sell), delta ratio, order book depth imbalance.
- **MarketProfileFilter** (`market_profile.py`): POC/VAH/VAL computation, value area (70% volume), prior session levels.
- **SupportResistanceFilter** (`support_resistance.py`): Prior session H/L, overnight H/L, weekly/monthly, custom levels. Strength scales with proximity.

### Services (`backend/services/`)
- **ConfidenceScorer** (`confidence_scorer.py`): `score(filter_results) → (float, dict)`. Formula: `clamp(0.5 + Σ(adjustment × weight), 0, 1)`. Default weight 0.25 per filter. Returns structured confluence_factors dict.
- **RiskManager** (`risk_manager.py`): `validate(signal, account, positions, qty) → RiskCheckResult`. 10 sequential checks (short-circuits on first failure):
  1. Naked order (SL+TP required)
  2. R:R ratio (1.5–5.0)
  3. No hedging
  4. Max concurrent positions (default 2)
  5. Max contracts (EVAL:8, PA:6, auto-reduces qty)
  6. Daily loss limit ($1,500)
  7. EVAL trailing drawdown (equity > eod_threshold)
  8. PA safety net (balance ≥ $103,100)
  9. PA 30% negative P&L rule
  10. PA SL/TP ratio ceiling (≤ 5:1)

### Database (`backend/database/`)
- **ORM models** (`models.py`): 7 tables — SignalRecord, TradeRecord (FK→signals), PhantomTradeRecord (FK→signals), AccountSnapshotRecord, EventRecord, CircuitBreakerEventRecord, ConfigHistoryRecord. UUID hex PKs.
- **Engine** (`db.py`): async SQLAlchemy with aiosqlite. `init_db()` / `drop_db()`.
- **Migrations** (`migrations/`): Alembic async-compatible. Initial migration creates all 7 tables.

### Config (`backend/config/`)
- **default_config.yaml**: All params — general (paper_mode, phase, trading hours, signal_expiration), instruments, apex_rules (eval/pa), strategies (3), filters (3 with weights), risk (rr bounds, confidence_rr_scale), circuit_breakers, news buffers, alerts.
- **settings.py**: Env vars for DATABASE_URL, BROKER_TYPE, Rithmic/Tradovate creds, server host/port.

### Frontend (`frontend/`)
Vite + React 18 + TypeScript SPA. Entry: `src/main.tsx` → `App.tsx` → `components/layout/Dashboard.tsx`.

**API layer** (`src/api/client.ts`):
- Axios instance with `baseURL = /api/v1`. All TypeScript types live here (Signal, Position, AccountInfo, StrategyInfo, FilterInfo, CircuitBreakerStatus).
- Dev proxy in `vite.config.ts`: `/api → http://localhost:8000`.

**Hooks** (`src/hooks/`):
- `useSignals(limit)` — GET /signals?limit=N, refetchInterval 2 s
- `useStrategies()` — GET /strategies (one-shot)
- `useToggleStrategy()` — POST /strategies/{name}/enable|disable mutation
- `usePositions()` — GET /positions (one-shot)
- `useAccount()` — GET /account (one-shot)
- `useCircuitBreaker()` — GET /circuit-breaker, refetchInterval 3 s
- `useTripCircuitBreaker()` — POST /circuit-breaker/trip { reason }
- `useResetCircuitBreaker()` — POST /circuit-breaker/reset

**Panels** (`src/components/panels/`):
- `SignalsPanel` — Tremor Table, 10 s countdown + ACCEPT/REJECT stubs (console.log) on newest decision=null signal
- `PositionsPanel` — empty-state placeholder
- `AccountPanel` — balance/equity/daily_pnl/phase/eod_threshold
- `StrategiesPanel` — Tremor Switch toggles calling enable/disable endpoints
- `FiltersPanel` — hardcoded fallback if API omits filters field
- `CircuitBreakerPanel` — trip (window.prompt for reason) + reset buttons
- `TradeHistoryPanel` — decided signals only, 20-per-page client-side pagination

**Tests** (`src/__tests__/`): Vitest + @testing-library/react, 4 test files covering SignalsPanel, CircuitBreakerPanel, StrategiesPanel, and all API hooks.

## Completed Tasks (1–15)
1. ✅ Core data models & broker ABC
2. ✅ Database layer (SQLAlchemy ORM, Alembic migrations)
3. ✅ Broker client implementations (Rithmic + Paper)
4. ✅ Three trading strategies (VWAP, EMA Crossover, Opening Range Breakout)
5. ✅ Three confluence filters (Order Flow, Market Profile, S/R)
6. ✅ Confidence scorer (aggregates filter outputs → 0-1 score)
7. ✅ Risk manager (Apex guardrails, R:R gating, 10 pre-trade checks)
8. ✅ Signal engine orchestrator (strategies → filters → scorer → risk manager pipeline)
9. ✅ Order manager (bracket order lifecycle via broker client)
10. ✅ Phantom tracker (rejected/expired signal outcome monitoring)
11. ✅ Session manager (trading hours enforcement, daily resets)
12. ✅ News service (economic calendar, signal suppression)
13. ✅ Circuit breaker (kill switch: consecutive losses, daily loss/profit, manual)
14. ✅ FastAPI backend (REST + WebSocket endpoints at /api/v1)
15. ✅ React dashboard (all 7 panels: signals, positions, account, strategies, filters, circuit breaker, trade history)

## Remaining Tasks (16–17)
16. **Integration testing** — Full paper-mode end-to-end test suite. Start FastAPI + PaperBrokerClient, feed synthetic bars through the signal engine, verify signals appear on the dashboard (or via API), accept a signal and confirm a bracket order is placed and tracked, let SL/TP hit and confirm P&L and PhantomTracker records. Cover circuit breaker trip/reset, session boundary behaviour, and risk manager guardrails firing correctly in the live pipeline.
17. **VPS deployment** — Docker, systemd, or similar.

## Conventions
- Frozen/slots dataclasses for performance on market data types
- ABC pattern for swappable implementations (BrokerClient, BaseStrategy)
- Filter evaluate() returns `{aligned, confidence_adjustment, details}` consistently
- All strategies return Signal | None from on_bar()
- Tests in `backend/tests/test_<module>.py`, one test class per component
- Params via dict with `default_params()` static method and `update_params()` for typo-safe updates
- pytest-asyncio auto mode (no explicit `@pytest.mark.asyncio` needed)
