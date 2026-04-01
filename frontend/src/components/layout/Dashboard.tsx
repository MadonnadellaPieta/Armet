import Header from "./Header";
import SignalsPanel from "../panels/SignalsPanel";
import PositionsPanel from "../panels/PositionsPanel";
import AccountPanel from "../panels/AccountPanel";
import StrategiesPanel from "../panels/StrategiesPanel";
import FiltersPanel from "../panels/FiltersPanel";
import CircuitBreakerPanel from "../panels/CircuitBreakerPanel";
import TradeHistoryPanel from "../panels/TradeHistoryPanel";

export default function Dashboard() {
  return (
    <div className="min-h-screen bg-gray-950">
      <Header />
      <main className="p-4 md:p-6 space-y-6">
        {/* Row 1: Signals full-width */}
        <SignalsPanel />

        {/* Row 2: Positions + Account */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          <PositionsPanel />
          <AccountPanel />
        </div>

        {/* Row 3: Strategies + Filters + Circuit Breaker */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
          <StrategiesPanel />
          <FiltersPanel />
          <CircuitBreakerPanel />
        </div>

        {/* Row 4: Trade History full-width */}
        <TradeHistoryPanel />
      </main>
    </div>
  );
}
