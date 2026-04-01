# CLAUDE.md — Futures Trading Signal System

## Project Overview
Semi-automated futures trading signal system with Python/FastAPI backend and React frontend.
Connects to Rithmic (async-rithmic) for real-time market data and order execution on ES/MES/NQ/MNQ futures.
Runs three configurable strategies that generate signals, displayed on a web dashboard with 10-second accept/reject window.
Accepted signals place bracket orders (entry + SL + TP) through Rithmic. Enforces Apex Trader Funding 100K EOD rules as non-overridable guardrails.

## Tech Stack
- **Backend**: Python 3.11+, FastAPI, SQLAlchemy 2.0 async, aiosqlite, Alembic
- **Frontend**: React 18+, Tremor, shadcn/ui, Tailwind CSS (not yet built)
- **Broker**: async-rithmic v1.5.9 (primary), Tradovate (fallback, not yet built)
- **Database**: SQLite via aiosqlite (v1)
- **Testing**: pytest with pytest-asyncio 0.23.8, `asyncio_mode = "auto"` in pyproject.toml

## Commands
- **Run all tests**: `python3.11 -m pytest backend/tests/ -v` (162 tests, all passing)
- **Run single test file**: `python3.11 -m pytest backend/tests/test_risk_manager.py -v`
- **Run integration tests only**: `python3.11 -m pytest backend/tests/test_integration.py -v`
- **Run migrations**: `cd /home/user/Armet && alembic upgrade head` (needs `data/` dir to exist)
- **Install deps**: `pip install -r requirements.txt`

## Branch
All development on: `claude/integration-testing-paper-mode-fZKKK`

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
- **SignalEngine** (`signal_engine.py`): Orchestrates strategies→filters→confidence→risk pipeline. `on_bar(Bar) → list[ApprovedSignal]`. Returns `ApprovedSignal(signal, approved_quantity, risk_details)`. Checks session_manager and circuit_breaker guards first. Persists SignalRecord (+ EventRecord on risk rejection) for every signal processed. Constructor: `SignalEngine(strategies, broker, session_factory, filters=None, scorer=None, risk_manager=None, circuit_breaker=None, session_manager=None)`.
- **OrderManager** (`order_manager.py`): `accept_signal(signal, qty) → TradeRecord` places bracket order, persists TradeRecord + updates SignalRecord decision. `reject_signal(signal) → PhantomTradeRecord` marks rejected and starts phantom tracking. `on_tick(tick)` resolves phantom TP/SL outcomes. On order fill callback: updates TradeRecord exit fields + writes AccountSnapshotRecord. Exposes `on_trade_close(callback)` for circuit breaker integration.
- **CircuitBreaker** (`circuit_breaker.py`): `is_tripped` property. `check_daily_loss(daily_pnl)`, `record_trade_result(pnl)`, `manual_trip()`. `reset()` clears tripped state. Persists CircuitBreakerEventRecord on trip. Constructor: `CircuitBreaker(session_factory, params)` with params: max_daily_loss=1500, max_consecutive_losses=3, max_daily_profit=5000.
- **SessionManager** (`session_manager.py`): `is_trading_hours(dt=None) → bool` checks 9:30–16:00 ET. `should_suppress_signal(dt=None) → bool`. `needs_daily_reset(dt=None) → bool`. `mark_daily_reset_done()`. Constructor accepts `now_fn` for test injection: `SessionManager(start_time, end_time, now_fn=None)`. Timezone: America/New_York via zoneinfo, naive datetimes assumed UTC.

### Database (`backend/database/`)
- **ORM models** (`models.py`): 7 tables — SignalRecord, TradeRecord (FK→signals), PhantomTradeRecord (FK→signals), AccountSnapshotRecord, EventRecord, CircuitBreakerEventRecord, ConfigHistoryRecord. UUID hex PKs.
- **Engine** (`db.py`): async SQLAlchemy with aiosqlite. `init_db()` / `drop_db()`.
- **Migrations** (`migrations/`): Alembic async-compatible. Initial migration creates all 7 tables.

### Config (`backend/config/`)
- **default_config.yaml**: All params — general (paper_mode, phase, trading hours, signal_expiration), instruments, apex_rules (eval/pa), strategies (3), filters (3 with weights), risk (rr bounds, confidence_rr_scale), circuit_breakers, news buffers, alerts.
- **settings.py**: Env vars for DATABASE_URL, BROKER_TYPE, Rithmic/Tradovate creds, server host/port.

## Completed Tasks (1–16)
1. ✅ Core data models & broker ABC
2. ✅ Database layer (SQLAlchemy ORM, Alembic migrations)
3. ✅ Broker client implementations (Rithmic + Paper)
4. ✅ Three trading strategies (VWAP, EMA Crossover, Opening Range Breakout)
5. ✅ Three confluence filters (Order Flow, Market Profile, S/R)
6. ✅ Confidence scorer (aggregates filter outputs → 0-1 score)
7. ✅ Risk manager (Apex guardrails, R:R gating, 10 pre-trade checks)
8. ✅ Signal engine orchestrator (`backend/services/signal_engine.py`) — `SignalEngine` class
9. ✅ Order manager (`backend/services/order_manager.py`) — `OrderManager` class with phantom tracking built-in
10. ✅ Phantom tracker — integrated into `OrderManager.reject_signal()` + `on_tick()`
11. ✅ Session manager (`backend/services/session_manager.py`) — `SessionManager` class
12. ⏭ News service — skipped (not needed for paper-mode; stub can be added in Task 17)
13. ✅ Circuit breaker (`backend/services/circuit_breaker.py`) — `CircuitBreaker` class
14. ⏭ FastAPI backend — stub exists at `backend/api/` (routes not yet wired for dashboard endpoints)
15. ⏭ React dashboard — not yet built
16. ✅ Integration testing (`backend/tests/test_integration.py`) — 7 paper-mode end-to-end tests

## Remaining Tasks (17)
17. **VPS deployment** — Docker, systemd, or similar. Also: finish FastAPI REST/WS endpoints (task 14) and React dashboard (task 15).

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

## Task 16 Implementation Notes (for context)

### New files added in Task 16
- `backend/services/signal_engine.py` — `SignalEngine` + `ApprovedSignal` dataclass
- `backend/services/order_manager.py` — `OrderManager` (accept/reject signals, phantom tracking, DB persistence)
- `backend/services/circuit_breaker.py` — `CircuitBreaker` (daily loss, consecutive losses, manual trip)
- `backend/services/session_manager.py` — `SessionManager` (ET trading hours, daily reset)
- `backend/tests/conftest.py` — shared fixtures: `db_engine`, `db_session`, `db_session_factory`, `paper_broker`
- `backend/tests/test_integration.py` — 7 end-to-end integration tests

### Integration wiring pattern (for FastAPI task 14)
```python
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine, AsyncSession
from backend.database.models import Base

engine = create_async_engine(DATABASE_URL)
sf = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

broker = PaperBrokerClient()
await broker.connect()

cb = CircuitBreaker(sf)
sm = SessionManager()
rm = RiskManager()
engine_svc = SignalEngine(strategies=[...], broker=broker, session_factory=sf,
                          circuit_breaker=cb, session_manager=sm, risk_manager=rm)
order_mgr = OrderManager(broker=broker, session_factory=sf)
order_mgr.on_trade_close(lambda sig_id, pnl: cb.check_daily_loss(...))
```

### What the FastAPI layer (Task 14) needs to expose
- `GET /api/v1/signals` — recent SignalRecords
- `POST /api/v1/signals/{id}/accept` — calls `order_mgr.accept_signal(signal, qty)`
- `POST /api/v1/signals/{id}/reject` — calls `order_mgr.reject_signal(signal)`
- `GET /api/v1/trades` — TradeRecord list
- `GET /api/v1/account` — broker.get_account_info()
- `GET /api/v1/positions` — broker.get_positions()
- `POST /api/v1/circuit-breaker/trip` — cb.manual_trip()
- `POST /api/v1/circuit-breaker/reset` — cb.reset()
- `WS /ws/signals` — push approved signals to dashboard (10-sec window)
