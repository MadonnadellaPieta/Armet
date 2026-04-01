# CLAUDE.md — Futures Trading Signal System

## Project Overview
Semi-automated futures trading signal system with Python/FastAPI backend and React frontend.
Connects to Rithmic (async-rithmic) for real-time market data and order execution on ES/MES/NQ/MNQ futures.
Runs three configurable strategies that generate signals, displayed on a web dashboard with 10-second accept/reject window.
Accepted signals place bracket orders (entry + SL + TP) through Rithmic. Enforces Apex Trader Funding 100K EOD rules as non-overridable guardrails.

## Tech Stack
- **Backend**: Python 3.11+, FastAPI, SQLAlchemy 2.0 async, aiosqlite, Alembic
- **Frontend**: React 18+, Tremor, Tailwind CSS, Vite, @tanstack/react-query
- **Broker**: async-rithmic v1.5.9 (primary), Tradovate (fallback, not yet built)
- **Database**: SQLite via aiosqlite (v1)
- **Testing**: pytest with pytest-asyncio 0.23.8, `asyncio_mode = "auto"` in pyproject.toml

## Commands
- **Run all tests**: `python3.11 -m pytest backend/tests/ -v` (182 tests, all passing)
- **Run single test file**: `python3.11 -m pytest backend/tests/test_api.py -v`
- **Run integration tests only**: `python3.11 -m pytest backend/tests/test_integration.py -v`
- **Run migrations**: `cd /home/user/Armet && alembic upgrade head` (needs `data/` dir to exist)
- **Install backend deps**: `pip install -r requirements.txt`
- **Install frontend deps**: `cd frontend && npm ci --legacy-peer-deps`
- **Build frontend**: `cd frontend && npm run build`
- **Run frontend tests**: `cd frontend && npm test`
- **Start dev server**: `uvicorn backend.main:app --reload` (backend on :8000, vite proxy in dev)
- **Docker build & run**: `docker compose up --build`

## Branch
All development on: `claude/vps-deployment-docker-9fGo8`

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
- **RiskManager** (`risk_manager.py`): `validate(signal, account, positions, qty) → RiskCheckResult`. 10 sequential checks (short-circuits on first failure).
- **SignalEngine** (`signal_engine.py`): Orchestrates strategies→filters→confidence→risk pipeline. `on_bar(Bar) → list[ApprovedSignal]`. Checks session_manager and circuit_breaker guards first. Persists SignalRecord (+ EventRecord on risk rejection). Constructor: `SignalEngine(strategies, broker, session_factory, filters=None, scorer=None, risk_manager=None, circuit_breaker=None, session_manager=None)`.
- **OrderManager** (`order_manager.py`): `accept_signal(signal, qty) → TradeRecord`, `reject_signal(signal) → PhantomTradeRecord`, `on_tick(tick)` for phantom resolution. Persists TradeRecord + AccountSnapshotRecord on close.
- **CircuitBreaker** (`circuit_breaker.py`): `is_tripped` property, `trip_reason` property. `check_daily_loss(daily_pnl)`, `record_trade_result(pnl)`, `manual_trip()`, `reset()`. Persists CircuitBreakerEventRecord on trip. Constructor: `CircuitBreaker(session_factory, params)`.
- **SessionManager** (`session_manager.py`): `is_trading_hours(dt=None) → bool` checks 9:30–16:00 ET. `should_suppress_signal(dt=None) → bool`. Constructor accepts `now_fn` for test injection.

### API Layer (`backend/api/`)
- **`app.py`** — `create_app()` factory: lifespan (init_db), CORS, routes, static frontend serving.
- **`deps.py`** — `get_db()` yields AsyncSession; `get_circuit_breaker()` returns process-level singleton.
- **`routers/signals.py`** — `GET /api/v1/signals?limit&offset`, `GET /api/v1/signals/{id}`. Maps `sl_price/tp_price` → `stop_loss_price/take_profit_price` in `SignalOut`.
- **`routers/account.py`** — `GET /api/v1/account` — last `AccountSnapshotRecord`.
- **`routers/positions.py`** — `GET /api/v1/positions` — returns `[]` (live positions via broker in future).
- **`routers/strategies.py`** — `GET /api/v1/strategies`, `POST /api/v1/strategies/{name}/enable|disable`.
- **`routers/circuit_breaker.py`** — `GET /api/v1/circuit-breaker`, `POST /api/v1/circuit-breaker/trip`, `POST /api/v1/circuit-breaker/reset`.

### Frontend (`frontend/`)
React 18 + Tremor + Tailwind + @tanstack/react-query + Vite.
- **7 panels**: SignalsPanel (countdown accept/reject), PositionsPanel, AccountPanel, StrategiesPanel, FiltersPanel, CircuitBreakerPanel, TradeHistoryPanel.
- **`src/api/client.ts`** — axios client with all TypeScript interface definitions matching backend API.
- **`src/hooks/`** — `useSignals`, `useStrategies`, `useAccount`, `usePositions`, `useCircuitBreaker` (all react-query, polling every 2–3s).
- Dev proxy: Vite proxies `/api` → `http://localhost:8000` so `npm run dev` works against running backend.
- Build output at `frontend/dist/` — served by FastAPI `StaticFiles` in production.

### Database (`backend/database/`)
- **ORM models** (`models.py`): 7 tables — SignalRecord, TradeRecord (FK→signals), PhantomTradeRecord (FK→signals), AccountSnapshotRecord, EventRecord, CircuitBreakerEventRecord, ConfigHistoryRecord. UUID hex PKs.
- **Engine** (`db.py`): async SQLAlchemy with aiosqlite. `init_db()` / `drop_db()`.
- **Migrations** (`migrations/`): Alembic async-compatible. Initial migration creates all 7 tables.

### Config (`backend/config/`)
- **default_config.yaml**: All params — general (paper_mode, phase, trading hours, signal_expiration), instruments, apex_rules (eval/pa), strategies (3), filters (3 with weights), risk (rr bounds, confidence_rr_scale), circuit_breakers, news buffers, alerts.
- **settings.py**: Env vars for DATABASE_URL, BROKER_TYPE, Rithmic/Tradovate creds, server host/port.

### Deployment (`deploy/` + root)
- **`Dockerfile`** — multi-stage: Node 20 builds frontend, Python 3.11-slim runs backend. Frontend dist copied into image; FastAPI serves it via `StaticFiles`.
- **`docker-compose.yml`** — single `armet` service, port 8000, `armet_data` volume for SQLite.
- **`deploy/armet.service`** — systemd unit for bare-metal VPS (alternative to Docker).
- **`deploy/setup-vps.sh`** — automated Ubuntu/Debian install script.

## Completed Tasks (1–17)
1. ✅ Core data models & broker ABC
2. ✅ Database layer (SQLAlchemy ORM, Alembic migrations)
3. ✅ Broker client implementations (Rithmic + Paper)
4. ✅ Three trading strategies (VWAP, EMA Crossover, Opening Range Breakout)
5. ✅ Three confluence filters (Order Flow, Market Profile, S/R)
6. ✅ Confidence scorer (aggregates filter outputs → 0-1 score)
7. ✅ Risk manager (Apex guardrails, R:R gating, 10 pre-trade checks)
8. ✅ Signal engine orchestrator (`backend/services/signal_engine.py`)
9. ✅ Order manager (`backend/services/order_manager.py`) with phantom tracking built-in
10. ✅ Phantom tracker — integrated into `OrderManager.reject_signal()` + `on_tick()`
11. ✅ Session manager (`backend/services/session_manager.py`)
12. ⏭ News service — skipped (not needed for paper-mode)
13. ✅ Circuit breaker (`backend/services/circuit_breaker.py`)
14. ✅ FastAPI backend (`backend/api/`) — 5 routers, 20 tests
15. ✅ React dashboard (`frontend/`) — 7 panels, Tremor + Tailwind, react-query polling
16. ✅ Integration testing (`backend/tests/test_integration.py`) — 7 end-to-end tests
17. ✅ VPS deployment — `Dockerfile` (multi-stage), `docker-compose.yml`, `deploy/armet.service` + `deploy/setup-vps.sh`

## Conventions
- Frozen/slots dataclasses for performance on market data types
- ABC pattern for swappable implementations (BrokerClient, BaseStrategy)
- Filter evaluate() returns `{aligned, confidence_adjustment, details}` consistently
- All strategies return Signal | None from on_bar()
- Tests in `backend/tests/test_<module>.py`, one test class per component
- Params via dict with `default_params()` static method and `update_params()` for typo-safe updates
- pytest-asyncio auto mode (no explicit `@pytest.mark.asyncio` needed)
- Services accept `session_factory: async_sessionmaker[AsyncSession]` (not the global engine from `db.py`)
- Use `python3.11 -m pytest` — the `pytest` binary uses a separate uv-managed venv without project deps
- API field mapping: DB uses `sl_price`/`tp_price`; API/frontend uses `stop_loss_price`/`take_profit_price`
