import { useGetTrades } from "@workspace/api-client-react";
import type { ActiveTrade } from "@workspace/api-client-react";
import { Link } from "wouter";
import { useState } from "react";
import {
  ArrowUpRight, ArrowDownRight, ArrowRight,
  TrendingUp, TrendingDown, Target, Shield, Clock, Timer, BarChart2
} from "lucide-react";

const INITIAL_TRADE_CARDS = 32;

function fmtAge(hrs: number | null | undefined): string {
  if (hrs == null) return "—";
  if (hrs < 1)  return `${Math.round(hrs * 60)}m`;
  if (hrs < 24) return `${hrs.toFixed(1)}h`;
  return `${(hrs / 24).toFixed(1)}d`;
}

function fmtEta(hrs: number | null | undefined): string {
  if (hrs == null) return "—";
  if (hrs < 0) return `Overdue ${fmtAge(Math.abs(hrs))}`;
  if (hrs < 1)  return `<1h`;
  if (hrs < 24) return `~${hrs.toFixed(0)}h`;
  return `~${(hrs / 24).toFixed(1)}d`;
}

function EtaBar({ age, expected }: { age: number | null | undefined; expected: number }) {
  if (age == null) return null;
  const pct = Math.min((age / expected) * 100, 100);
  const overdue = age > expected;
  return (
    <div className="w-full h-1 bg-muted rounded-full overflow-hidden mt-1">
      <div
        className={`h-full rounded-full transition-all ${overdue ? "bg-signal-sell" : pct > 75 ? "bg-yellow-500" : "bg-primary"}`}
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

export default function Trades() {
  const { data, isLoading } = useGetTrades({
    query: { refetchInterval: 15000, queryKey: ["/api/trades"] }
  });

  const trades = data?.trades ?? [];
  const [visibleCount, setVisibleCount] = useState(INITIAL_TRADE_CARDS);
  const intraday = trades.filter((t: ActiveTrade) => (t.intraday_or_swing as string) === "Intraday");
  const swing    = trades.filter((t: ActiveTrade) => (t.intraday_or_swing as string) === "Swing");

  return (
    <div className="h-full flex flex-col bg-background overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between p-4 border-b border-border shrink-0">
        <div className="flex items-center gap-4">
          <h1 className="text-lg font-bold tracking-tight">Active Positions</h1>
          <div className="flex gap-2">
            <span className="px-2 py-0.5 bg-cyan-500/10 border border-cyan-500/30 text-cyan-400 text-[10px] font-bold rounded tracking-wider">
              {intraday.length} INTRADAY
            </span>
            <span className="px-2 py-0.5 bg-purple-500/10 border border-purple-500/30 text-purple-400 text-[10px] font-bold rounded tracking-wider">
              {swing.length} SWING
            </span>
          </div>
        </div>
        <div className="flex gap-4">
          <div className="flex flex-col items-end">
            <span className="text-[10px] uppercase text-muted-foreground font-semibold">Long</span>
            <span className="font-mono text-sm text-signal-buy font-bold">{data?.long_count ?? 0}</span>
          </div>
          <div className="flex flex-col items-end">
            <span className="text-[10px] uppercase text-muted-foreground font-semibold">Short</span>
            <span className="font-mono text-sm text-signal-sell font-bold">{data?.short_count ?? 0}</span>
          </div>
        </div>
      </div>

      <div className="flex-1 overflow-auto p-4">
        {isLoading ? (
          <div className="h-full flex items-center justify-center text-muted-foreground">
            <span className="font-mono text-sm animate-pulse">Loading trades...</span>
          </div>
        ) : trades.length === 0 ? (
          <div className="h-full flex flex-col items-center justify-center text-muted-foreground bg-card/50 rounded-lg border border-border border-dashed">
            <Target className="h-10 w-10 mb-4 opacity-50" />
            <p className="font-mono text-sm">No active trades</p>
            <p className="text-xs opacity-70 mt-1">Waiting for signal triggers.</p>
          </div>
        ) : (
          <div className="space-y-4">
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
            {trades.slice(0, visibleCount).map((trade: ActiveTrade & Record<string, unknown>, i) => {
              const isProfitable = trade.pnl_pct >= 0;
              const PnlIcon = isProfitable ? TrendingUp : TrendingDown;
              const isSwing = (trade.intraday_or_swing as string) === "Swing";
              const dailyMove = trade.daily_move_pct as number | null | undefined;
              const liveMovePct = trade.live_move_pct as number | null | undefined;
              const liveMovePoints = trade.live_move_pts as number | null | undefined;
              const tradeAge = trade.trade_age_hrs as number | null | undefined;
              const etaHrs = trade.eta_hrs as number | null | undefined;
              const expectedDur = (trade.expected_duration_hrs as number) ?? 24;
              const tsl = trade.tsl as number | null | undefined;

              return (
                <div
                  key={`${trade.symbol}-${trade.timeframe}`}
                  className="bg-card border border-border rounded-lg overflow-hidden flex flex-col"
                  style={{ contentVisibility: "auto", containIntrinsicSize: "420px" }}
                >
                  {/* Card Header */}
                  <div className="p-3 border-b border-border flex justify-between items-start bg-muted/20">
                    <div>
                      <div className="flex items-center gap-2">
                        <h3 className="font-bold font-mono text-base">{trade.symbol}</h3>
                        <div className="flex gap-1">
                          <span className="px-1.5 py-0.5 bg-background border border-border rounded text-[10px] font-mono text-muted-foreground">
                            {trade.timeframe}
                          </span>
                          <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold tracking-wider ${
                            isSwing
                              ? "bg-purple-500/10 border border-purple-500/30 text-purple-400"
                              : "bg-cyan-500/10 border border-cyan-500/30 text-cyan-400"
                          }`}>
                            {isSwing ? "SWING" : "INTRA"}
                          </span>
                        </div>
                      </div>
                      <div className={`mt-1 inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-bold tracking-widest ${
                        trade.direction === "BUY"
                          ? "bg-signal-buy/20 text-signal-buy"
                          : "bg-signal-sell/20 text-signal-sell"
                      }`}>
                        {trade.direction === "BUY" ? <ArrowUpRight className="h-3 w-3" /> : <ArrowDownRight className="h-3 w-3" />}
                        {trade.direction}
                      </div>
                    </div>

                    {/* P&L */}
                    <div className="text-right">
                      <div className={`flex items-center justify-end gap-1 font-mono text-lg font-bold ${isProfitable ? "text-signal-buy" : "text-signal-sell"}`}>
                        <PnlIcon className="h-4 w-4" />
                        {trade.pnl_pct > 0 ? "+" : ""}{trade.pnl_pct.toFixed(2)}%
                      </div>
                      <div className={`text-xs font-mono ${isProfitable ? "text-signal-buy/70" : "text-signal-sell/70"}`}>
                        {(trade.pnl_points > 0 ? "+" : "")}{trade.pnl_points.toFixed(2)} pts
                      </div>
                      {dailyMove != null && (
                        <div className={`text-[10px] font-mono mt-0.5 ${dailyMove > 0 ? "text-signal-buy/60" : "text-signal-sell/60"}`}>
                          Day: {dailyMove > 0 ? "+" : ""}{dailyMove.toFixed(2)}%
                        </div>
                      )}
                    </div>
                  </div>

                  {/* Prices grid */}
                  <div className="p-3 flex-1 flex flex-col gap-3">
                    <div className="grid grid-cols-2 gap-y-2 gap-x-4">
                      <div>
                        <span className="text-[10px] uppercase text-muted-foreground font-semibold block mb-0.5">Entry</span>
                        <span className="font-mono text-sm tabular-nums">{trade.entry_price.toFixed(2)}</span>
                      </div>
                      <div>
                        <span className="text-[10px] uppercase text-muted-foreground font-semibold block mb-0.5">Current</span>
                        <span className={`font-mono text-sm tabular-nums font-bold ${isProfitable ? "text-signal-buy" : "text-signal-sell"}`}>
                          {trade.current_price.toFixed(2)}
                        </span>
                      </div>

                      {/* SL */}
                      <div>
                        <span className="text-[10px] uppercase text-signal-sell/70 font-semibold flex items-center gap-1 mb-0.5">
                          <Shield className="h-3 w-3" /> SL1
                        </span>
                        <span className="font-mono text-sm tabular-nums text-signal-sell/80">{trade.sl1.toFixed(2)}</span>
                      </div>

                      {/* TSL */}
                      <div>
                        <span className="text-[10px] uppercase text-signal-sell/50 font-semibold flex items-center gap-1 mb-0.5">
                          <Shield className="h-3 w-3" /> TSL
                        </span>
                        <span className="font-mono text-sm tabular-nums text-signal-sell/60">
                          {tsl != null ? tsl.toFixed(2) : <span className="text-muted-foreground">—</span>}
                        </span>
                      </div>

                      {/* TP1 */}
                      <div>
                        <span className="text-[10px] uppercase text-signal-buy/70 font-semibold flex items-center gap-1 mb-0.5">
                          <Target className="h-3 w-3" /> T1
                        </span>
                        <span className={`font-mono text-sm tabular-nums ${trade.t1_hit ? "line-through text-signal-buy/40" : "text-signal-buy/80"}`}>
                          {trade.tp1.toFixed(2)}
                        </span>
                      </div>

                      {/* TP2 */}
                      {trade.tp2 != null && (
                        <div>
                          <span className="text-[10px] uppercase text-signal-buy/60 font-semibold flex items-center gap-1 mb-0.5">
                            <Target className="h-3 w-3" /> T2
                          </span>
                          <span className={`font-mono text-sm tabular-nums ${trade.t2_hit ? "line-through text-signal-buy/40" : "text-signal-buy/60"}`}>
                            {trade.tp2.toFixed(2)}
                          </span>
                        </div>
                      )}

                      {/* TP3 */}
                      {trade.tp3 != null && (
                        <div>
                          <span className="text-[10px] uppercase text-signal-buy/50 font-semibold flex items-center gap-1 mb-0.5">
                            <Target className="h-3 w-3" /> T3
                          </span>
                          <span className={`font-mono text-sm tabular-nums ${trade.t3_hit ? "line-through text-signal-buy/40" : "text-signal-buy/50"}`}>
                            {trade.tp3.toFixed(2)}
                          </span>
                        </div>
                      )}
                    </div>

                    {/* Milestone badges */}
                    <div className="flex items-center gap-1.5 pt-1 border-t border-border/50">
                      {[
                        { label: "T1", hit: trade.t1_hit },
                        { label: "T2", hit: trade.t2_hit },
                        { label: "T3", hit: trade.t3_hit },
                      ].map(m => (
                        <div key={m.label} className={`flex-1 text-center py-1 rounded text-xs font-bold transition-colors ${
                          m.hit
                            ? "bg-signal-buy/20 text-signal-buy border border-signal-buy/30"
                            : "bg-muted text-muted-foreground border border-border"
                        }`}>
                          {m.label} {m.hit ? "✓" : ""}
                        </div>
                      ))}
                    </div>

                    {/* Live R:R */}
                    {trade.live_rr != null && (
                      <div className="flex justify-between items-center text-xs">
                        <span className="text-muted-foreground uppercase font-semibold">Live R:R</span>
                        <span className={`font-mono font-bold ${trade.live_rr >= 1 ? "text-signal-buy" : "text-signal-sell"}`}>
                          1:{trade.live_rr.toFixed(2)}
                        </span>
                      </div>
                    )}

                    {/* Trade age + ETA */}
                    <div className="space-y-1">
                      <div className="flex justify-between text-[10px] text-muted-foreground">
                        <span className="flex items-center gap-1">
                          <Clock className="h-3 w-3" /> Age: {fmtAge(tradeAge)}
                        </span>
                        <span className="flex items-center gap-1">
                          <Timer className="h-3 w-3" /> ETA: {fmtEta(etaHrs)}
                        </span>
                      </div>
                      <EtaBar age={tradeAge} expected={expectedDur} />
                    </div>
                  </div>

                  {/* Card Footer */}
                  <div className="px-3 py-2 border-t border-border bg-muted/10 flex justify-between items-center">
                    <span className="text-[10px] text-muted-foreground font-mono flex items-center gap-1">
                      <BarChart2 className="h-3 w-3" />
                      {trade.setup || "—"}
                    </span>
                    <Link
                      href={`/chart/${encodeURIComponent(trade.symbol)}/${trade.timeframe}`}
                      className="text-xs text-primary hover:text-primary/80 font-medium flex items-center"
                    >
                      Chart <ArrowRight className="h-3 w-3 ml-1" />
                    </Link>
                  </div>
                </div>
              );
            })}
            </div>
            {visibleCount < trades.length && (
              <div className="flex justify-center">
                <button
                  type="button"
                  onClick={() => setVisibleCount((count) => Math.min(count + INITIAL_TRADE_CARDS, trades.length))}
                  className="px-4 py-2 rounded-md border border-border bg-card text-sm font-medium text-foreground hover:bg-muted transition-colors"
                >
                  Show more positions ({trades.length - visibleCount} remaining)
                </button>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
