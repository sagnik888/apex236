import { useState, useMemo } from "react";
import { useMutation } from "@tanstack/react-query";

const API_BASE = "";

interface BacktestParams {
  symbol?: string | null;
  timeframe: string;
  days: number;
  min_score: number;
  conflict_margin: number;
  atr_mult: number;
  fixed_sl_pct: number;
  fixed_tp_pct: number;
  force_fixed_sl: boolean;
  exit_at_t1: boolean;
  slippage_pct: number;
  cost_pct: number;
  include_options: boolean;
}

interface Trade {
  symbol: string;
  timeframe: string;
  direction: string;
  entry_time: string;
  entry_price: number;
  exit_time?: string;
  exit_price?: number;
  exit_reason?: string;
  gross_pnl_pct?: number;
  net_pnl_pct?: number;
  hold_bars?: number;
  won?: boolean;
  score: number;
  setup: string;
  sl1: number;
  tp1: number;
  option_type?: string;
  option_strike?: number | null;
}

interface ScoreBand {
  band: string;
  count: number;
  win_rate_pct: number;
  avg_pnl_pct: number;
  total_pnl_pct: number;
}

interface BacktestResult {
  summary: {
    total_trades: number;
    wins: number;
    losses: number;
    win_rate_pct: number;
    total_pnl_pct: number;
    avg_pnl_pct: number;
    profit_factor: number;
    sharpe_ratio: number;
    max_drawdown_pct: number;
    symbols_tested: number;
    symbols_with_trades: number;
    errors: number;
    timeframe: string;
    days: number;
  };
  parameters: Record<string, unknown>;
  trades: Trade[];
  equity_curve: { time: string; equity: number; symbol: string; pnl: number }[];
  score_bands: ScoreBand[];
  direction_breakdown: {
    long: { count: number; win_rate_pct: number; avg_pnl_pct: number; total_pnl_pct: number };
    short: { count: number; win_rate_pct: number; avg_pnl_pct: number; total_pnl_pct: number };
  };
  setup_breakdown: { setup: string; count: number; win_rate_pct: number; avg_pnl_pct: number; total_pnl_pct: number }[];
  symbol_summaries: { symbol: string; total_trades: number; wins: number; losses: number; win_rate_pct: number; total_pnl_pct: number; avg_pnl_pct: number }[];
  errors: string[];
}

async function runBacktest(params: BacktestParams): Promise<BacktestResult> {
  const res = await fetch(`${API_BASE}/api/backtest`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
  if (!res.ok) throw new Error(`Backtest failed: ${res.status}`);
  return res.json();
}

function MetricCard({ label, value, sub, color }: { label: string; value: string | number; sub?: string; color?: string }) {
  return (
    <div className="bg-card border border-border rounded-lg p-3">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className={`text-lg font-bold ${color ?? "text-foreground"}`}>{value}</p>
      {sub && <p className="text-xs text-muted-foreground">{sub}</p>}
    </div>
  );
}

export default function Backtest() {
  const [params, setParams] = useState<BacktestParams>({
    symbol: null,
    timeframe: "15m",
    days: 60,
    min_score: 60,
    conflict_margin: 10,
    atr_mult: 2.0,
    fixed_sl_pct: 0,
    fixed_tp_pct: 0,
    force_fixed_sl: false,
    exit_at_t1: false,
    slippage_pct: 0.05,
    cost_pct: 0.182,
    include_options: false,
  });

  const [symbolInput, setSymbolInput] = useState("");
  const [activeTab, setActiveTab] = useState<"summary" | "trades" | "scores" | "symbols">("summary");

  const mutation = useMutation({
    mutationFn: runBacktest,
  });

  const result = mutation.data;

  const handleRun = () => {
    const p = { ...params, symbol: symbolInput.trim() || null };
    mutation.mutate(p);
  };

  // Equity curve SVG
  const equitySvg = useMemo(() => {
    if (!result?.equity_curve?.length) return null;
    const pts = result.equity_curve;
    const vals = pts.map((p) => p.equity);
    const minY = Math.min(0, ...vals);
    const maxY = Math.max(0, ...vals);
    const range = maxY - minY || 1;
    const w = 800;
    const h = 200;
    const points = pts.map((p, i) => `${(i / (pts.length - 1)) * w},${h - ((p.equity - minY) / range) * h}`).join(" ");
    const zeroY = h - ((0 - minY) / range) * h;
    return (
      <svg viewBox={`0 0 ${w} ${h}`} className="w-full h-48 mt-2" preserveAspectRatio="none">
        <line x1="0" y1={zeroY} x2={w} y2={zeroY} stroke="currentColor" strokeOpacity={0.2} strokeDasharray="4" />
        <polyline fill="none" stroke="hsl(var(--primary))" strokeWidth="2" points={points} />
      </svg>
    );
  }, [result?.equity_curve]);

  return (
    <div className="p-4 space-y-4 max-w-[1400px] mx-auto">
      <h1 className="text-xl font-bold">📊 Backtest Portal</h1>

      {/* ── Controls ── */}
      <div className="bg-card border border-border rounded-lg p-4">
        <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3">
          <div>
            <label className="text-xs text-muted-foreground block mb-1">Symbol (blank = all)</label>
            <input
              className="w-full bg-background border border-border rounded px-2 py-1 text-sm"
              placeholder="e.g. RELIANCE"
              value={symbolInput}
              onChange={(e) => setSymbolInput(e.target.value.toUpperCase())}
            />
          </div>
          <div>
            <label className="text-xs text-muted-foreground block mb-1">Timeframe</label>
            <select className="w-full bg-background border border-border rounded px-2 py-1 text-sm" value={params.timeframe} onChange={(e) => setParams((p) => ({ ...p, timeframe: e.target.value }))}>
              <option value="5m">5m</option>
              <option value="15m">15m</option>
              <option value="1h">1h</option>
            </select>
          </div>
          <div>
            <label className="text-xs text-muted-foreground block mb-1">Days</label>
            <input type="number" className="w-full bg-background border border-border rounded px-2 py-1 text-sm" value={params.days} onChange={(e) => setParams((p) => ({ ...p, days: +e.target.value }))} />
          </div>
          <div>
            <label className="text-xs text-muted-foreground block mb-1">Min Score</label>
            <input type="number" className="w-full bg-background border border-border rounded px-2 py-1 text-sm" value={params.min_score} onChange={(e) => setParams((p) => ({ ...p, min_score: +e.target.value }))} />
          </div>
          <div>
            <label className="text-xs text-muted-foreground block mb-1">ATR Mult</label>
            <input type="number" step="0.1" className="w-full bg-background border border-border rounded px-2 py-1 text-sm" value={params.atr_mult} onChange={(e) => setParams((p) => ({ ...p, atr_mult: +e.target.value }))} />
          </div>
          <div>
            <label className="text-xs text-muted-foreground block mb-1">Conflict Margin</label>
            <input type="number" className="w-full bg-background border border-border rounded px-2 py-1 text-sm" value={params.conflict_margin} onChange={(e) => setParams((p) => ({ ...p, conflict_margin: +e.target.value }))} />
          </div>
          <div>
            <label className="text-xs text-muted-foreground block mb-1">Slippage %</label>
            <input type="number" step="0.01" className="w-full bg-background border border-border rounded px-2 py-1 text-sm" value={params.slippage_pct} onChange={(e) => setParams((p) => ({ ...p, slippage_pct: +e.target.value }))} />
          </div>
          <div>
            <label className="text-xs text-muted-foreground block mb-1">Cost %</label>
            <input type="number" step="0.01" className="w-full bg-background border border-border rounded px-2 py-1 text-sm" value={params.cost_pct} onChange={(e) => setParams((p) => ({ ...p, cost_pct: +e.target.value }))} />
          </div>
          <div>
            <label className="text-xs text-muted-foreground block mb-1">Fixed SL %</label>
            <input type="number" step="0.1" className="w-full bg-background border border-border rounded px-2 py-1 text-sm" value={params.fixed_sl_pct} onChange={(e) => setParams((p) => ({ ...p, fixed_sl_pct: +e.target.value }))} />
          </div>
          <div>
            <label className="text-xs text-muted-foreground block mb-1">Fixed TP %</label>
            <input type="number" step="0.1" className="w-full bg-background border border-border rounded px-2 py-1 text-sm" value={params.fixed_tp_pct} onChange={(e) => setParams((p) => ({ ...p, fixed_tp_pct: +e.target.value }))} />
          </div>
          <div className="flex items-end gap-2">
            <label className="flex items-center gap-1 text-xs text-muted-foreground">
              <input type="checkbox" checked={params.exit_at_t1} onChange={(e) => setParams((p) => ({ ...p, exit_at_t1: e.target.checked }))} />
              Exit @ T1
            </label>
          </div>
          <div className="flex items-end">
            <button
              onClick={handleRun}
              disabled={mutation.isPending}
              className="w-full bg-primary text-primary-foreground rounded px-4 py-1.5 text-sm font-medium hover:bg-primary/90 disabled:opacity-50"
            >
              {mutation.isPending ? "Running…" : "▶ Run Backtest"}
            </button>
          </div>
        </div>
      </div>

      {/* ── Status ── */}
      {mutation.isPending && (
        <div className="text-center py-8 text-muted-foreground animate-pulse">
          ⏳ Running backtest{symbolInput ? ` on ${symbolInput}` : " across all 236 symbols"}… This may take 30-120 seconds.
        </div>
      )}
      {mutation.isError && (
        <div className="bg-destructive/10 text-destructive border border-destructive/20 rounded-lg p-3 text-sm">
          ❌ {(mutation.error as Error).message}
        </div>
      )}

      {/* ── Results ── */}
      {result && (
        <>
          {/* Summary Cards */}
          <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-8 gap-2">
            <MetricCard label="Total Trades" value={result.summary.total_trades} />
            <MetricCard label="Win Rate" value={`${result.summary.win_rate_pct}%`} color={result.summary.win_rate_pct >= 40 ? "text-green-400" : "text-red-400"} />
            <MetricCard label="Total P&L" value={`${result.summary.total_pnl_pct}%`} color={result.summary.total_pnl_pct >= 0 ? "text-green-400" : "text-red-400"} />
            <MetricCard label="Avg P&L" value={`${result.summary.avg_pnl_pct}%`} color={result.summary.avg_pnl_pct >= 0 ? "text-green-400" : "text-red-400"} />
            <MetricCard label="Profit Factor" value={result.summary.profit_factor} color={result.summary.profit_factor >= 1 ? "text-green-400" : "text-red-400"} />
            <MetricCard label="Sharpe" value={result.summary.sharpe_ratio} color={result.summary.sharpe_ratio >= 0 ? "text-green-400" : "text-red-400"} />
            <MetricCard label="Max DD" value={`${result.summary.max_drawdown_pct}%`} color="text-red-400" />
            <MetricCard label="Symbols" value={`${result.summary.symbols_with_trades}/${result.summary.symbols_tested}`} sub={`${result.summary.errors} errors`} />
          </div>

          {/* Equity Curve */}
          <div className="bg-card border border-border rounded-lg p-4">
            <h2 className="text-sm font-semibold mb-1">Equity Curve (Cumulative P&L %)</h2>
            {equitySvg ?? <p className="text-xs text-muted-foreground">No trades to chart</p>}
          </div>

          {/* Tab navigation */}
          <div className="flex gap-1 bg-muted rounded-lg p-1">
            {(["summary", "trades", "scores", "symbols"] as const).map((tab) => (
              <button
                key={tab}
                onClick={() => setActiveTab(tab)}
                className={`px-3 py-1 text-xs font-medium rounded-md transition ${activeTab === tab ? "bg-background text-foreground shadow" : "text-muted-foreground hover:text-foreground"}`}
              >
                {tab === "summary" ? "Breakdown" : tab === "trades" ? "Trade Log" : tab === "scores" ? "Score Bands" : "By Symbol"}
              </button>
            ))}
          </div>

          {/* Tab content */}
          {activeTab === "summary" && (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {/* Direction breakdown */}
              <div className="bg-card border border-border rounded-lg p-4">
                <h3 className="text-sm font-semibold mb-2">Direction Breakdown</h3>
                <table className="w-full text-xs">
                  <thead><tr className="text-muted-foreground border-b border-border"><th className="text-left py-1">Dir</th><th>Count</th><th>Win%</th><th>Avg P&L</th><th>Total</th></tr></thead>
                  <tbody>
                    {(["long", "short"] as const).map((d) => {
                      const s = result.direction_breakdown[d];
                      return (
                        <tr key={d} className="border-b border-border/50">
                          <td className="py-1 font-medium">{d === "long" ? "🟢 LONG" : "🔴 SHORT"}</td>
                          <td className="text-center">{s.count}</td>
                          <td className="text-center">{s.win_rate_pct}%</td>
                          <td className={`text-center ${s.avg_pnl_pct >= 0 ? "text-green-400" : "text-red-400"}`}>{s.avg_pnl_pct}%</td>
                          <td className={`text-center ${s.total_pnl_pct >= 0 ? "text-green-400" : "text-red-400"}`}>{s.total_pnl_pct}%</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              {/* Setup breakdown */}
              <div className="bg-card border border-border rounded-lg p-4">
                <h3 className="text-sm font-semibold mb-2">Setup Breakdown</h3>
                <table className="w-full text-xs">
                  <thead><tr className="text-muted-foreground border-b border-border"><th className="text-left py-1">Setup</th><th>Count</th><th>Win%</th><th>Avg P&L</th><th>Total</th></tr></thead>
                  <tbody>
                    {result.setup_breakdown.map((s) => (
                      <tr key={s.setup} className="border-b border-border/50">
                        <td className="py-1">{s.setup}</td>
                        <td className="text-center">{s.count}</td>
                        <td className="text-center">{s.win_rate_pct}%</td>
                        <td className={`text-center ${s.avg_pnl_pct >= 0 ? "text-green-400" : "text-red-400"}`}>{s.avg_pnl_pct}%</td>
                        <td className={`text-center ${s.total_pnl_pct >= 0 ? "text-green-400" : "text-red-400"}`}>{s.total_pnl_pct}%</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {activeTab === "trades" && (
            <div className="bg-card border border-border rounded-lg p-4 overflow-x-auto">
              <h3 className="text-sm font-semibold mb-2">Trade Log ({result.trades.length} trades{result.trades.length >= 500 ? " — showing first 500" : ""})</h3>
              <table className="w-full text-xs whitespace-nowrap">
                <thead>
                  <tr className="text-muted-foreground border-b border-border">
                    <th className="text-left py-1 pr-2">Symbol</th>
                    <th>Dir</th>
                    <th>Score</th>
                    <th>Setup</th>
                    <th>Entry</th>
                    <th>Exit</th>
                    <th>SL</th>
                    <th>TP1</th>
                    <th>Exit Reason</th>
                    <th>Bars</th>
                    <th>Net P&L</th>
                    <th>Entry Time</th>
                  </tr>
                </thead>
                <tbody>
                  {result.trades.map((t, i) => (
                    <tr key={i} className={`border-b border-border/30 ${t.won ? "bg-green-500/5" : "bg-red-500/5"}`}>
                      <td className="py-1 pr-2 font-medium">{t.symbol}</td>
                      <td className={`text-center ${t.direction === "BUY" ? "text-green-400" : "text-red-400"}`}>{t.direction}</td>
                      <td className="text-center">{t.score?.toFixed(0)}</td>
                      <td className="text-center">{t.setup}</td>
                      <td className="text-center">₹{t.entry_price?.toFixed(1)}</td>
                      <td className="text-center">{t.exit_price ? `₹${t.exit_price.toFixed(1)}` : "—"}</td>
                      <td className="text-center">{t.sl1?.toFixed(1)}</td>
                      <td className="text-center">{t.tp1?.toFixed(1)}</td>
                      <td className="text-center text-xs">{t.exit_reason ?? "—"}</td>
                      <td className="text-center">{t.hold_bars ?? "—"}</td>
                      <td className={`text-center font-medium ${(t.net_pnl_pct ?? 0) >= 0 ? "text-green-400" : "text-red-400"}`}>
                        {t.net_pnl_pct != null ? `${t.net_pnl_pct.toFixed(2)}%` : "—"}
                      </td>
                      <td className="text-muted-foreground">{t.entry_time?.slice(0, 16)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {activeTab === "scores" && (
            <div className="bg-card border border-border rounded-lg p-4">
              <h3 className="text-sm font-semibold mb-2">Performance by Score Band</h3>
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-muted-foreground border-b border-border">
                    <th className="text-left py-1">Band</th><th>Count</th><th>Win Rate</th><th>Avg P&L</th><th>Total P&L</th>
                  </tr>
                </thead>
                <tbody>
                  {result.score_bands.map((b) => (
                    <tr key={b.band} className="border-b border-border/50">
                      <td className="py-1.5 font-medium">{b.band}</td>
                      <td className="text-center">{b.count}</td>
                      <td className={`text-center ${b.win_rate_pct >= 40 ? "text-green-400" : "text-red-400"}`}>{b.win_rate_pct}%</td>
                      <td className={`text-center ${b.avg_pnl_pct >= 0 ? "text-green-400" : "text-red-400"}`}>{b.avg_pnl_pct}%</td>
                      <td className={`text-center font-medium ${b.total_pnl_pct >= 0 ? "text-green-400" : "text-red-400"}`}>{b.total_pnl_pct}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {activeTab === "symbols" && (
            <div className="bg-card border border-border rounded-lg p-4 overflow-x-auto">
              <h3 className="text-sm font-semibold mb-2">Per-Symbol Results ({result.symbol_summaries.length} symbols)</h3>
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-muted-foreground border-b border-border">
                    <th className="text-left py-1">Symbol</th><th>Trades</th><th>Wins</th><th>Losses</th><th>Win%</th><th>Avg P&L</th><th>Total P&L</th>
                  </tr>
                </thead>
                <tbody>
                  {result.symbol_summaries
                    .sort((a, b) => b.total_pnl_pct - a.total_pnl_pct)
                    .map((s) => (
                      <tr key={s.symbol} className="border-b border-border/30">
                        <td className="py-1 font-medium">{s.symbol}</td>
                        <td className="text-center">{s.total_trades}</td>
                        <td className="text-center text-green-400">{s.wins}</td>
                        <td className="text-center text-red-400">{s.losses}</td>
                        <td className="text-center">{s.win_rate_pct}%</td>
                        <td className={`text-center ${s.avg_pnl_pct >= 0 ? "text-green-400" : "text-red-400"}`}>{s.avg_pnl_pct}%</td>
                        <td className={`text-center font-medium ${s.total_pnl_pct >= 0 ? "text-green-400" : "text-red-400"}`}>{s.total_pnl_pct}%</td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Errors */}
          {result.errors.length > 0 && (
            <details className="bg-card border border-border rounded-lg p-3">
              <summary className="text-xs text-muted-foreground cursor-pointer">⚠ {result.errors.length} symbol errors</summary>
              <pre className="text-xs text-muted-foreground mt-2 max-h-32 overflow-y-auto">{result.errors.join("\n")}</pre>
            </details>
          )}
        </>
      )}
    </div>
  );
}
