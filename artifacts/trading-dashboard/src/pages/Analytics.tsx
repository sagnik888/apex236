import { useState } from "react";
import { useGetAnalytics } from "@workspace/api-client-react";
import { 
  BarChart3, TrendingUp, TrendingDown, Award, Target, Shield, 
  Clock, Zap, Activity, PieChart, Layers, ArrowUpRight, ArrowDownRight,
  Filter, RefreshCw, CheckCircle2, AlertCircle
} from "lucide-react";

export default function Analytics() {
  const [selectedTf, setSelectedTf] = useState<string>("ALL");
  const [selectedTenure, setSelectedTenure] = useState<string>("30d");
  const { data, isLoading, isError, refetch, isRefetching } = useGetAnalytics(
    { tenure: selectedTenure },
    {
      query: { refetchInterval: 30000, queryKey: ["/api/analytics", { tenure: selectedTenure }] }
    }
  );

  if (isLoading) {
    return (
      <div className="flex h-full w-full items-center justify-center bg-background p-8">
        <div className="flex flex-col items-center gap-3">
          <RefreshCw className="h-8 w-8 animate-spin text-primary" />
          <span className="text-sm font-mono text-muted-foreground">Aggregating real-time strategy analytics across 236 symbols...</span>
        </div>
      </div>
    );
  }

  if (isError || !data) {
    return (
      <div className="flex h-full w-full items-center justify-center bg-background p-8">
        <div className="flex flex-col items-center gap-4 max-w-md text-center">
          <AlertCircle className="h-10 w-10 text-destructive" />
          <h3 className="text-lg font-bold text-foreground">Failed to Load Performance Analytics</h3>
          <p className="text-sm text-muted-foreground">Check your backend connection or refresh the page to reload real-time trade records.</p>
          <button 
            onClick={() => refetch()}
            className="px-4 py-2 bg-primary text-primary-foreground rounded-md text-sm font-medium hover:bg-primary/90 transition-colors"
          >
            Retry Analytics
          </button>
        </div>
      </div>
    );
  }

  const summary = data.summary || {};
  const tfBreakdown = data.timeframe_breakdown || {};
  const stratBreakdown = data.strategy_breakdown || {};
  const periodBreakdown = data.period_breakdown || {};
  const equityCurve = data.equity_curve || [];
  const sectorList = data.sector_performance || [];

  const currentTfData = tfBreakdown[selectedTf] || tfBreakdown["ALL"] || {};

  return (
    <div className="flex flex-col h-full w-full bg-background overflow-y-auto p-4 md:p-6 gap-6">
      {/* Header Panel */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 border-b border-border pb-4">
        <div>
          <div className="flex items-center gap-2">
            <BarChart3 className="h-6 w-6 text-primary" />
            <h1 className="text-2xl font-black tracking-tight text-foreground">Strategy Performance & Real-Time Analytics</h1>
          </div>
          <p className="text-xs font-mono text-muted-foreground mt-1">
            Aggregated real data across {summary.total_symbols || 236} NSE symbols • Filter: {summary.selected_tenure || "30D"} (Max Limit: {summary.max_tenure_limit || "365D"}) • Last check: {summary.last_scan || "Live"}
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {/* Tenure Filter Pills */}
          <div className="flex items-center bg-card border border-border rounded-lg p-1 gap-1">
            <span className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground px-1.5 flex items-center gap-1 hidden md:flex">
              <Clock className="h-3 w-3" /> Tenure:
            </span>
            {[
              { id: "1d", label: "1D" },
              { id: "7d", label: "7D" },
              { id: "30d", label: "30D" },
              { id: "90d", label: "90D" },
              { id: "180d", label: "180D" },
              { id: "365d", label: "365D (Max)" }
            ].map((item) => (
              <button
                key={item.id}
                onClick={() => setSelectedTenure(item.id)}
                className={`px-2 py-1 text-xs font-bold rounded-md transition-all ${
                  selectedTenure === item.id
                    ? "bg-primary text-primary-foreground shadow-sm"
                    : "text-muted-foreground hover:text-foreground hover:bg-muted"
                }`}
              >
                {item.label}
              </button>
            ))}
          </div>

          <button
            onClick={() => refetch()}
            disabled={isRefetching}
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium border border-border rounded-md bg-card hover:bg-muted text-foreground transition-all disabled:opacity-50"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${isRefetching ? "animate-spin" : ""}`} />
            Refresh
          </button>
        </div>
      </div>

      {/* Top Level KPI Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        {/* Overall Win Rate */}
        <div className="flex flex-col justify-between p-4 bg-card border border-border rounded-xl shadow-sm hover:border-primary/40 transition-all">
          <div className="flex items-center justify-between">
            <span className="text-xs uppercase font-bold tracking-wider text-muted-foreground">Overall Win Rate</span>
            <div className="p-2 rounded-lg bg-emerald-500/10 text-emerald-400">
              <Award className="h-5 w-5" />
            </div>
          </div>
          <div className="mt-3 flex items-baseline justify-between">
            <span className="text-3xl font-black font-mono tracking-tight text-foreground">
              {summary.overall_win_rate_pct ?? 0}%
            </span>
            <span className="text-xs font-mono text-emerald-400 font-semibold">
              {tfBreakdown["ALL"]?.wins ?? 0}W / {tfBreakdown["ALL"]?.losses ?? 0}L
            </span>
          </div>
          <div className="mt-2 w-full bg-muted rounded-full h-1.5 overflow-hidden">
            <div 
              className="bg-emerald-500 h-full rounded-full transition-all duration-500" 
              style={{ width: `${Math.min(100, Math.max(0, summary.overall_win_rate_pct ?? 0))}%` }}
            />
          </div>
        </div>

        {/* Profit Factor */}
        <div className="flex flex-col justify-between p-4 bg-card border border-border rounded-xl shadow-sm hover:border-primary/40 transition-all">
          <div className="flex items-center justify-between">
            <span className="text-xs uppercase font-bold tracking-wider text-muted-foreground">Profit Factor</span>
            <div className="p-2 rounded-lg bg-blue-500/10 text-blue-400">
              <TrendingUp className="h-5 w-5" />
            </div>
          </div>
          <div className="mt-3 flex items-baseline justify-between">
            <span className="text-3xl font-black font-mono tracking-tight text-foreground">
              {summary.overall_profit_factor ?? 0}x
            </span>
            <span className="text-xs font-mono text-blue-400 font-semibold">
              Payoff: {tfBreakdown["ALL"]?.payoff_ratio ?? 0}
            </span>
          </div>
          <p className="text-[11px] font-mono text-muted-foreground mt-2">
            Gross Profit vs Gross Loss efficiency ratio
          </p>
        </div>

        {/* Sharpe Ratio */}
        <div className="flex flex-col justify-between p-4 bg-card border border-border rounded-xl shadow-sm hover:border-primary/40 transition-all">
          <div className="flex items-center justify-between">
            <span className="text-xs uppercase font-bold tracking-wider text-muted-foreground">Trade Sharpe (per-trade)</span>
            <div className="p-2 rounded-lg bg-purple-500/10 text-purple-400">
              <Target className="h-5 w-5" />
            </div>
          </div>
          <div className="mt-3 flex items-baseline justify-between">
            <span className="text-3xl font-black font-mono tracking-tight text-foreground">
              {summary.overall_sharpe_ratio ?? 0}
            </span>
            <span className="text-xs font-mono text-purple-400 font-semibold">
              Half Kelly: {tfBreakdown["ALL"]?.half_kelly_pct ?? 0}%
            </span>
          </div>
          <p className="text-[11px] font-mono text-muted-foreground mt-2">
            Risk-adjusted return velocity across TFs
          </p>
        </div>

        {/* Total Trades Analyzed */}
        <div className="flex flex-col justify-between p-4 bg-card border border-border rounded-xl shadow-sm hover:border-primary/40 transition-all">
          <div className="flex items-center justify-between">
            <span className="text-xs uppercase font-bold tracking-wider text-muted-foreground">Universe Activity</span>
            <div className="p-2 rounded-lg bg-amber-500/10 text-amber-400">
              <Activity className="h-5 w-5" />
            </div>
          </div>
          <div className="mt-3 flex items-baseline justify-between">
            <span className="text-3xl font-black font-mono tracking-tight text-foreground">
              {summary.total_historical_trades ?? 0}
            </span>
            <span className="text-xs font-mono text-amber-400 font-semibold">
              {summary.total_active_trades ?? 0} Active Now
            </span>
          </div>
          <p className="text-[11px] font-mono text-muted-foreground mt-2">
            Live signals: {summary.total_signals ?? 0} across universe
          </p>
        </div>
      </div>

      {/* Timeframe-wise Win Rate & Metrics Matrix & Deep-Dive */}
      <div className="bg-card border border-border rounded-xl p-4 md:p-6 shadow-sm space-y-6">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
          <div>
            <h2 className="text-lg font-bold text-foreground flex items-center gap-2">
              <Layers className="h-5 w-5 text-primary" />
              Timeframe-Wise Performance Matrix
            </h2>
            <p className="text-xs text-muted-foreground">
              Compare win rate, Sharpe ratio, profit factor, and trade counts side-by-side across all trading horizons
            </p>
          </div>

          <div className="flex items-center gap-1 bg-muted/40 p-1 rounded-lg border border-border shrink-0">
            {["ALL", "15m", "1h", "4h", "1d"].map((tf) => (
              <button
                key={tf}
                onClick={() => setSelectedTf(tf)}
                className={`px-3 py-1.5 rounded-md text-xs font-bold transition-all ${
                  selectedTf === tf
                    ? "bg-primary text-primary-foreground shadow-sm"
                    : "text-muted-foreground hover:text-foreground"
                }`}
              >
                {tf === "ALL" ? "Combined (All)" : tf}
              </button>
            ))}
          </div>
        </div>

        {/* Side-by-Side Comparative Matrix Table */}
        <div className="overflow-x-auto rounded-xl border border-border/70 bg-muted/10">
          <table className="w-full text-left border-collapse text-xs">
            <thead>
              <tr className="border-b border-border/70 bg-muted/30 text-muted-foreground font-semibold uppercase text-[10px] tracking-wider">
                <th className="py-3 px-4">Timeframe / Horizon</th>
                <th className="py-3 px-4 text-right">Trades (Closed)</th>
                <th className="py-3 px-4 text-right">Win Rate %</th>
                <th className="py-3 px-4 text-right">Sharpe Ratio</th>
                <th className="py-3 px-4 text-right">Profit Factor</th>
                <th className="py-3 px-4 text-right">Gross PnL (W/L)</th>
                <th className="py-3 px-4 text-right">Payoff Ratio</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border/40 font-mono">
              {["ALL", "15m", "1h", "4h", "1d"].map((tfKey) => {
                const tfRow = tfBreakdown[tfKey] || {};
                const isSelected = selectedTf === tfKey;
                const wr = tfRow.win_rate_pct ?? 0;
                const sh = tfRow.sharpe_ratio ?? 0;
                const pf = tfRow.profit_factor ?? 0;
                const wins = tfRow.wins ?? 0;
                const losses = tfRow.losses ?? 0;
                const total = tfRow.num_trades ?? 0;

                return (
                  <tr
                    key={tfKey}
                    onClick={() => setSelectedTf(tfKey)}
                    className={`cursor-pointer transition-colors ${
                      isSelected
                        ? "bg-primary/10 border-l-2 border-l-primary"
                        : "hover:bg-muted/20"
                    }`}
                  >
                    <td className="py-3.5 px-4 font-bold font-sans flex items-center gap-2">
                      <span className="text-sm text-foreground">{tfKey === "ALL" ? "Combined (All)" : tfKey}</span>
                      {tfKey !== "ALL" && (
                        <span className={`px-1.5 py-0.5 rounded text-[9px] font-black uppercase tracking-wider ${
                          tfKey === "15m" || tfKey === "1h"
                            ? "bg-cyan-500/10 text-cyan-400 border border-cyan-500/30"
                            : "bg-purple-500/10 text-purple-400 border border-purple-500/30"
                        }`}>
                          {tfKey === "15m" || tfKey === "1h" ? "Intraday" : "Swing"}
                        </span>
                      )}
                    </td>
                    <td className="py-3.5 px-4 text-right text-foreground font-bold">
                      {total} <span className="text-muted-foreground font-normal">({wins}W/{losses}L)</span>
                    </td>
                    <td className="py-3.5 px-4 text-right">
                      <div className="flex items-center justify-end gap-2">
                        <span className={`font-black ${wr >= 50 ? "text-emerald-400" : "text-amber-400"}`}>{wr}%</span>
                        <div className="w-12 bg-muted rounded-full h-1.5 overflow-hidden hidden sm:block">
                          <div className="bg-emerald-500 h-full rounded-full" style={{ width: `${Math.min(100, Math.max(0, wr))}%` }} />
                        </div>
                      </div>
                    </td>
                    <td className="py-3.5 px-4 text-right">
                      <span className={`font-bold ${sh >= 1.5 ? "text-emerald-400" : sh >= 1.0 ? "text-blue-400" : "text-foreground"}`}>
                        {sh}
                      </span>
                    </td>
                    <td className="py-3.5 px-4 text-right">
                      <span className={`font-bold ${pf >= 1.5 ? "text-emerald-400" : pf >= 1.2 ? "text-blue-400" : "text-foreground"}`}>
                        {pf}x
                      </span>
                    </td>
                    <td className="py-3.5 px-4 text-right text-xs">
                      <span className="text-emerald-400">+{tfRow.avg_win_pct ?? 0}%</span>
                      <span className="text-muted-foreground mx-1">/</span>
                      <span className="text-rose-400">-{tfRow.avg_loss_pct ?? 0}%</span>
                    </td>
                    <td className="py-3.5 px-4 text-right text-muted-foreground font-bold">
                      {tfRow.payoff_ratio ?? 0}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        {/* Selected Timeframe Deep-Dive Details Card */}
        <div className="bg-muted/15 p-4 rounded-xl border border-border/60">
          <div className="text-xs font-bold text-muted-foreground uppercase mb-3 flex items-center justify-between">
            <span>Deep-Dive Metrics for: <strong className="text-primary font-mono">{currentTfData.timeframe || selectedTf}</strong></span>
            <span className="text-[11px] font-mono font-normal">Half Kelly Criterion: <strong className="text-foreground">{currentTfData.half_kelly_pct ?? 0}%</strong></span>
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-4">
            <div className="flex flex-col p-3 rounded-lg bg-card/60 border border-border/40">
              <span className="text-[10px] uppercase text-muted-foreground font-semibold">Timeframe</span>
              <span className="text-xl font-black font-mono text-primary mt-1">{currentTfData.timeframe || selectedTf}</span>
            </div>
            <div className="flex flex-col p-3 rounded-lg bg-card/60 border border-border/40">
              <span className="text-[10px] uppercase text-muted-foreground font-semibold">Trades Count</span>
              <span className="text-xl font-black font-mono text-foreground mt-1">{currentTfData.num_trades ?? 0}</span>
            </div>
            <div className="flex flex-col p-3 rounded-lg bg-card/60 border border-border/40">
              <span className="text-[10px] uppercase text-muted-foreground font-semibold">Win Rate</span>
              <span className={`text-xl font-black font-mono mt-1 ${
                (currentTfData.win_rate_pct ?? 0) >= 50 ? "text-emerald-400" : "text-amber-400"
              }`}>
                {currentTfData.win_rate_pct ?? 0}%
              </span>
            </div>
            <div className="flex flex-col p-3 rounded-lg bg-card/60 border border-border/40">
              <span className="text-[10px] uppercase text-muted-foreground font-semibold">Sharpe Ratio</span>
              <span className="text-xl font-black font-mono text-purple-400 mt-1">{currentTfData.sharpe_ratio ?? 0}</span>
            </div>
            <div className="flex flex-col p-3 rounded-lg bg-card/60 border border-border/40">
              <span className="text-[10px] uppercase text-muted-foreground font-semibold">Profit Factor</span>
              <span className="text-xl font-black font-mono text-blue-400 mt-1">{currentTfData.profit_factor ?? 0}x</span>
            </div>
            <div className="flex flex-col p-3 rounded-lg bg-card/60 border border-border/40">
              <span className="text-[10px] uppercase text-muted-foreground font-semibold">Avg Win/Loss Ratio</span>
              <span className="text-base font-black font-mono text-foreground mt-1.5 leading-tight">
                <span className="text-emerald-400">+{currentTfData.avg_win_pct ?? 0}%</span> / <span className="text-rose-400">-{currentTfData.avg_loss_pct ?? 0}%</span>
              </span>
            </div>
          </div>
        </div>
      </div>

      {/* Strategy Type & Period Breakdown Section */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Strategy Type Comparison (Intraday vs Swing vs BTST/STBT) */}
        <div className="lg:col-span-2 bg-card border border-border rounded-xl p-4 md:p-6 shadow-sm flex flex-col justify-between">
          <div>
            <h2 className="text-lg font-bold text-foreground flex items-center gap-2">
              <PieChart className="h-5 w-5 text-primary" />
              Trade Type & Horizon Comparison
            </h2>
            <p className="text-xs text-muted-foreground mb-4">
              Real execution split across Intraday, Multi-day Swing, and Overnight BTST/STBT setups
            </p>

            <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
              {Object.entries(stratBreakdown).map(([key, strat]: [string, any]) => {
                const isIntra = key === "INTRADAY";
                const isSwing = key === "SWING";
                const badgeColor = isIntra 
                  ? "border-cyan-500/40 bg-cyan-500/10 text-cyan-400" 
                  : isSwing 
                    ? "border-purple-500/40 bg-purple-500/10 text-purple-400" 
                    : "border-amber-500/40 bg-amber-500/10 text-amber-400";

                return (
                  <div key={key} className="flex flex-col justify-between p-4 rounded-xl border border-border bg-muted/10 hover:bg-muted/20 transition-all">
                    <div>
                      <div className="flex items-center justify-between">
                        <span className={`px-2.5 py-1 rounded text-[11px] font-black border uppercase tracking-wider ${badgeColor}`}>
                          {strat.category?.replace("_", "/") || key}
                        </span>
                        <span className="text-xs font-mono text-muted-foreground">{strat.num_trades ?? 0} trades</span>
                      </div>

                      <div className="mt-4 flex items-baseline justify-between">
                        <div>
                          <div className="text-[11px] uppercase text-muted-foreground font-semibold">Win Rate</div>
                          <div className="text-2xl font-black font-mono text-foreground mt-0.5">{strat.win_rate_pct ?? 0}%</div>
                        </div>
                        <div className="text-right">
                          <div className="text-[11px] uppercase text-muted-foreground font-semibold">Total PnL</div>
                          <div className={`text-xl font-black font-mono mt-0.5 ${
                            (strat.total_pnl_pct ?? 0) >= 0 ? "text-emerald-400" : "text-rose-400"
                          }`}>
                            {(strat.total_pnl_pct ?? 0) >= 0 ? "+" : ""}{strat.total_pnl_pct ?? 0}%
                          </div>
                        </div>
                      </div>
                    </div>

                    <div className="mt-4 pt-3 border-t border-border/60 flex items-center justify-between text-xs font-mono text-muted-foreground">
                      <span>PF: <strong className="text-foreground">{strat.profit_factor ?? 0}x</strong></span>
                      <span>Sharpe: <strong className="text-foreground">{strat.sharpe_ratio ?? 0}</strong></span>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        </div>

        {/* Daily / Weekly / Monthly Real PnL Breakdown */}
        <div className="bg-card border border-border rounded-xl p-4 md:p-6 shadow-sm flex flex-col justify-between">
          <div>
            <h2 className="text-lg font-bold text-foreground flex items-center gap-2">
              <Clock className="h-5 w-5 text-primary" />
              Period Return Snapshot
            </h2>
            <p className="text-xs text-muted-foreground mb-4">
              Realized + live active equity contribution across trading windows
            </p>

            <div className="flex flex-col gap-3">
              {Object.entries(periodBreakdown).map(([key, item]: [string, any]) => (
                <div key={key} className="flex items-center justify-between p-3.5 rounded-lg border border-border bg-muted/20">
                  <div>
                    <div className="text-sm font-bold text-foreground">{item.period || key}</div>
                    <div className="text-xs font-mono text-muted-foreground mt-0.5">
                      {item.trades_closed ?? 0} trades closed
                    </div>
                  </div>

                  <div className="text-right font-mono">
                    <div className={`text-base font-black ${
                      (item.pnl_pct ?? 0) >= 0 ? "text-emerald-400" : "text-rose-400"
                    }`}>
                      {(item.pnl_pct ?? 0) >= 0 ? "+" : ""}{item.pnl_pct ?? 0}%
                    </div>
                    {/* points-per-share sum, not INR — no position sizing yet */}
                    {item.live_abs_inr !== undefined && (
                      <div className="text-[11px] text-muted-foreground">
                        Live: {item.live_abs_inr} pts (unsized)
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>

      {/* 30-Day Cumulative Equity Trajectory & Sector Leaderboard */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* SVG Equity Chart */}
        <div className="lg:col-span-2 bg-card border border-border rounded-xl p-4 md:p-6 shadow-sm flex flex-col">
          <div className="flex items-center justify-between mb-4">
            <div>
              <h2 className="text-lg font-bold text-foreground flex items-center gap-2">
                <TrendingUp className="h-5 w-5 text-emerald-400" />
                30-Day Cumulative Equity Trajectory
              </h2>
              <p className="text-xs text-muted-foreground">
                Normalized index growth anchored on daily realized returns and open positions
              </p>
            </div>
          </div>

          <div className="flex-1 min-h-[220px] w-full bg-muted/10 rounded-xl border border-border/50 p-4 flex flex-col justify-end relative overflow-hidden">
            {/* SVG Trajectory Render */}
            {equityCurve.length > 1 ? (
              <svg className="w-full h-44 overflow-visible" viewBox="0 0 100 100" preserveAspectRatio="none">
                <defs>
                  <linearGradient id="equityGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#22c55e" stopOpacity="0.35" />
                    <stop offset="100%" stopColor="#22c55e" stopOpacity="0.0" />
                  </linearGradient>
                </defs>
                {/* Area path */}
                <path
                  d={(() => {
                    const vals = equityCurve.map((e: any) => e.equity_index || 100);
                    const minV = Math.min(...vals) * 0.99;
                    const maxV = Math.max(...vals) * 1.01;
                    const rng = maxV - minV || 1;
                    const pts = vals.map((v: number, i: number) => {
                      const x = (i / (vals.length - 1)) * 100;
                      const y = 100 - ((v - minV) / rng) * 90 - 5;
                      return `${x},${y}`;
                    });
                    return `M 0,100 L ${pts[0]} ` + pts.map((p: string) => `L ${p}`).join(" ") + ` L 100,100 Z`;
                  })()}
                  fill="url(#equityGrad)"
                />
                {/* Line path */}
                <path
                  d={(() => {
                    const vals = equityCurve.map((e: any) => e.equity_index || 100);
                    const minV = Math.min(...vals) * 0.99;
                    const maxV = Math.max(...vals) * 1.01;
                    const rng = maxV - minV || 1;
                    const pts = vals.map((v: number, i: number) => {
                      const x = (i / (vals.length - 1)) * 100;
                      const y = 100 - ((v - minV) / rng) * 90 - 5;
                      return `${x},${y}`;
                    });
                    return `M ` + pts.join(" L ");
                  })()}
                  fill="none"
                  stroke="#22c55e"
                  strokeWidth="2.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            ) : (
              <div className="flex h-full w-full items-center justify-center text-xs font-mono text-muted-foreground">
                Collecting daily return history across scans...
              </div>
            )}

            <div className="flex items-center justify-between text-[11px] font-mono text-muted-foreground mt-2 pt-2 border-t border-border/40">
              <span>{equityCurve[0]?.date || "30 Days Ago"}</span>
              <span className="text-emerald-400 font-bold">
                Index: {equityCurve[equityCurve.length - 1]?.equity_index || 100} ({
                  round((equityCurve[equityCurve.length - 1]?.equity_index || 100) - 100, 2)
                }%)
              </span>
              <span>{equityCurve[equityCurve.length - 1]?.date || "Today"}</span>
            </div>
          </div>
        </div>

        {/* Sector Leaderboard */}
        <div className="bg-card border border-border rounded-xl p-4 md:p-6 shadow-sm flex flex-col">
          <div className="mb-4">
            <h2 className="text-lg font-bold text-foreground flex items-center gap-2">
              <Award className="h-5 w-5 text-amber-400" />
              Sector Performance
            </h2>
            <p className="text-xs text-muted-foreground">
              Win rate & return distribution across Nifty sectors
            </p>
          </div>

          <div className="flex-1 overflow-y-auto max-h-[260px] pr-1 space-y-2.5">
            {sectorList.map((sec: any, idx: number) => (
              <div key={sec.sector || idx} className="flex items-center justify-between p-2.5 rounded-lg border border-border/60 bg-muted/15 hover:bg-muted/30 transition-all">
                <div className="flex items-center gap-2.5">
                  <span className="text-xs font-mono font-bold text-muted-foreground w-5">{idx + 1}.</span>
                  <div>
                    <div className="text-xs font-bold text-foreground">{sec.sector || "NSE"}</div>
                    <div className="text-[10px] font-mono text-muted-foreground">{sec.trades_count ?? 0} trades • WR {sec.win_rate_pct ?? 0}%</div>
                  </div>
                </div>

                <div className="text-right font-mono">
                  <div className={`text-xs font-black ${
                    (sec.total_pnl_pct ?? 0) >= 0 ? "text-emerald-400" : "text-rose-400"
                  }`}>
                    {(sec.total_pnl_pct ?? 0) >= 0 ? "+" : ""}{sec.total_pnl_pct ?? 0}%
                  </div>
                  <div className="text-[10px] text-muted-foreground">
                    Sharpe: {sec.sharpe_ratio ?? 0}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function round(val: number, decimals: number): number {
  return Number(Math.round(Number(val + "e" + decimals)) + "e-" + decimals);
}
