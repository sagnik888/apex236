import { useState, useMemo, useEffect } from "react";
import { useGetSignals, useGetScannerStats, useTriggerScan } from "@workspace/api-client-react";
import type { Signal } from "@workspace/api-client-react";
import { useLocation } from "wouter";
import { Activity, ArrowUpRight, ArrowDownRight, RefreshCw, BarChart2, ChevronUp, ChevronDown, ChevronsUpDown } from "lucide-react";
import { Button } from "@/components/ui/button";

type SortKey = "score" | "close" | "rsi" | "adx" | "pnl_pct" | "daily_move_pct" | "eta_hrs" | "signal_time";
type SortDir = "asc" | "desc";

function SortIcon({ col, active, dir }: { col: string; active: string; dir: SortDir }) {
  if (col !== active) return <ChevronsUpDown className="h-3 w-3 opacity-30" />;
  return dir === "asc" ? <ChevronUp className="h-3 w-3 text-primary" /> : <ChevronDown className="h-3 w-3 text-primary" />;
}

function getRegimeStyle(value?: string) {
  if (!value) return { bg: "bg-muted/30", fg: "text-muted-foreground", border: "border-border", letter: "-" };
  const isBull = value.includes("T+");
  const isBear = value.includes("T-");
  const isVol = value.includes("V") || value.includes("v");

  if (isBull && isVol) {
    return { bg: "bg-blue-500/20", fg: "text-blue-400 font-bold", border: "border-blue-500/40 shadow-[0_0_6px_rgba(59,130,246,0.3)]", letter: "V+" };
  }
  if (isBear && isVol) {
    return { bg: "bg-yellow-500/20", fg: "text-yellow-400 font-bold", border: "border-yellow-500/40 shadow-[0_0_6px_rgba(234,179,8,0.3)]", letter: "V-" };
  }
  if (isBull) {
    return { bg: "bg-signal-buy/20", fg: "text-signal-buy font-bold", border: "border-signal-buy/30", letter: "B" };
  }
  if (isBear) {
    return { bg: "bg-signal-sell/20", fg: "text-signal-sell font-bold", border: "border-signal-sell/30", letter: "S" };
  }
  return { bg: "bg-gray-500/20", fg: "text-gray-400 font-bold", border: "border-gray-500/30", letter: "R" };
}

function RegimeBox({ value, label, activeStatus }: { value?: string; label: string; activeStatus?: string }) {
  const lineClass = activeStatus === "PROFIT" ? "bg-signal-buy" : activeStatus === "LOSS" ? "bg-signal-sell" : "bg-white";
  const { bg, fg, border, letter } = getRegimeStyle(value);

  if (!value) return (
    <div className="w-6 h-5 bg-muted/30 rounded border border-border flex items-center justify-center text-[8px] text-muted-foreground opacity-50 relative" title={label}>
      {label}
      {activeStatus && <div className={`absolute -bottom-1 left-[1px] right-[1px] h-[2px] ${lineClass} rounded-full`} />}
    </div>
  );

  return (
    <div className={`w-6 h-5 rounded border flex items-center justify-center text-[9px] relative ${bg} ${fg} ${border}`} title={`${label}: ${value}`}>
      {letter}
      {activeStatus && (
        <div className={`absolute -bottom-[3px] left-[1px] right-[1px] h-[2px] ${lineClass} rounded-full shadow-sm`} />
      )}
    </div>
  );
}

type MoodType = "high-bull" | "high-bear" | "bullish" | "bearish" | "neutral";

interface MoodInfo {
  mood: MoodType;
  label: string;
  badgeClass: string;
  textClass: string;
  rowClass: string;
  borderClass: string;
}

function getRegimeMoodInfo(row: any): MoodInfo {
  const tf = row.timeframe || "15m";
  const regimeStr = (row[`regime_${tf}`] || row.regime_15m || "") as string;
  
  const isBull = regimeStr.includes("T+") || row.direction === "BUY";
  const isBear = regimeStr.includes("T-") || row.direction === "SELL";
  const isHighMom = regimeStr.includes("V") || regimeStr.includes("v") || (row.adx != null && row.adx >= 25 && (regimeStr.includes("T+") || regimeStr.includes("T-")));
  const isRanging = regimeStr.includes("R") || (row.adx != null && row.adx < 20);

  if (isBull && isHighMom && !isRanging) {
    return {
      mood: "high-bull",
      label: "High Bull Momentum",
      badgeClass: "bg-blue-500/20 text-blue-400 border border-blue-500/40 shadow-[0_0_8px_rgba(59,130,246,0.25)]",
      textClass: "text-blue-400 font-bold",
      rowClass: "bg-blue-500/[0.04] hover:bg-blue-500/[0.12]",
      borderClass: "border-l-4 border-l-blue-400",
    };
  }
  if (isBear && isHighMom && !isRanging) {
    return {
      mood: "high-bear",
      label: "High Bear Momentum",
      badgeClass: "bg-yellow-500/20 text-yellow-400 border border-yellow-500/40 shadow-[0_0_8px_rgba(234,179,8,0.25)]",
      textClass: "text-yellow-400 font-bold",
      rowClass: "bg-yellow-500/[0.04] hover:bg-yellow-500/[0.12]",
      borderClass: "border-l-4 border-l-yellow-400",
    };
  }
  if (isBull && !isRanging) {
    return {
      mood: "bullish",
      label: "Bullish Trend",
      badgeClass: "bg-signal-buy/20 text-signal-buy border border-signal-buy/30",
      textClass: "text-signal-buy font-semibold",
      rowClass: "bg-signal-buy/[0.03] hover:bg-signal-buy/[0.10]",
      borderClass: "border-l-4 border-l-signal-buy",
    };
  }
  if (isBear && !isRanging) {
    return {
      mood: "bearish",
      label: "Bearish Trend",
      badgeClass: "bg-signal-sell/20 text-signal-sell border border-signal-sell/30",
      textClass: "text-signal-sell font-semibold",
      rowClass: "bg-signal-sell/[0.03] hover:bg-signal-sell/[0.10]",
      borderClass: "border-l-4 border-l-signal-sell",
    };
  }
  return {
    mood: "neutral",
    label: "Neutral / Ranging",
    badgeClass: "bg-gray-500/20 text-gray-300 border border-gray-500/30",
    textClass: "text-gray-400 font-medium",
    rowClass: "bg-muted/5 hover:bg-muted/20",
    borderClass: "border-l-4 border-l-gray-600/50",
  };
}

function formatSignalTime(ts: string | unknown): string {
  if (!ts || typeof ts !== "string") return "—";
  try {
    const d = new Date(ts);
    if (isNaN(d.getTime())) return ts; // fallback
    const hh = String(d.getHours()).padStart(2, '0');
    const mm = String(d.getMinutes()).padStart(2, '0');
    const dd = String(d.getDate()).padStart(2, '0');
    const mo = String(d.getMonth() + 1).padStart(2, '0');
    return `${hh}:${mm}/${dd}:${mo}`;
  } catch (e) {
    return ts;
  }
}

export default function Dashboard() {
  const [timeframe, setTimeframe] = useState<string>(() => localStorage.getItem("apex_timeframe") || "ALL");
  const [filter, setFilter] = useState<string>(() => localStorage.getItem("apex_filter") || "ALL");
  const [tradeTypeFilter, setTradeTypeFilter] = useState<string>(() => localStorage.getItem("apex_trade_type_filter") || "ALL");
  const [sortKey, setSortKey] = useState<SortKey>("signal_time");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  // Save to localStorage when filters change
  useEffect(() => {
    localStorage.setItem("apex_timeframe", timeframe);
  }, [timeframe]);

  useEffect(() => {
    localStorage.setItem("apex_filter", filter);
  }, [filter]);

  useEffect(() => {
    localStorage.setItem("apex_trade_type_filter", tradeTypeFilter);
  }, [tradeTypeFilter]);

  const [, setLocation] = useLocation();

  const { data: stats } = useGetScannerStats({
    query: { refetchInterval: 15000, queryKey: ["/api/stats"] }
  });

  const { data: signalsData, isLoading } = useGetSignals(
    {
      timeframe: timeframe === "ALL" ? undefined : timeframe,
      direction: filter === "ALL" ? undefined : filter,
    },
    { query: { refetchInterval: 15000, queryKey: ["/api/signals", timeframe, filter] } }
  );

  const triggerScan = useTriggerScan();

  const handleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDir(d => d === "asc" ? "desc" : "asc");
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  };

  const raw = signalsData?.signals ?? [];

  const signals = useMemo(() => {
    let copy = [...raw];
    
    // Apply trade type filter
    if (tradeTypeFilter !== "ALL") {
      copy = copy.filter(s => {
        // "BTST" or "STBT" or "Swing" should pass for "Swing" if we group them, 
        // but user asked for intraday vs swing vs both. 
        // Our engine returns "Intraday", "Swing", "BTST", "STBT".
        const type = (s as any).intraday_or_swing || "";
        const transition = (s as any).transition || "";
        if (tradeTypeFilter === "INTRADAY") {
          return type === "Intraday" && transition !== "BTST";
        } else if (tradeTypeFilter === "SWING") {
          return type !== "Intraday"; // Swing, BTST, STBT
        } else if (tradeTypeFilter === "BTST") {
          return transition === "BTST";
        }
        return true;
      });
    }

    copy.sort((a: Signal & Record<string, unknown>, b: Signal & Record<string, unknown>) => {
      if (sortKey === "signal_time") {
        const av = (a.signal_time as string) || "";
        const bv = (b.signal_time as string) || "";
        return sortDir === "asc" ? av.localeCompare(bv) : bv.localeCompare(av);
      }
      let av = a[sortKey] as number | null | undefined;
      let bv = b[sortKey] as number | null | undefined;
      av = av ?? (sortDir === "asc" ? Infinity : -Infinity);
      bv = bv ?? (sortDir === "asc" ? Infinity : -Infinity);
      return sortDir === "asc" ? av - bv : bv - av;
    });
    return copy;
  }, [raw, sortKey, sortDir, tradeTypeFilter]);

  const Th = ({ label, col, className = "" }: { label: string; col?: SortKey; className?: string }) => (
    <th
      className={`px-4 py-3 font-medium text-left ${col ? "cursor-pointer hover:text-foreground select-none" : ""} ${className}`}
      onClick={col ? () => handleSort(col) : undefined}
    >
      <div className={`flex items-center gap-1 ${className.includes("right") ? "justify-end" : ""}`}>
        {label}
        {col && <SortIcon col={col} active={sortKey} dir={sortDir} />}
      </div>
    </th>
  );

  return (
    <div className="h-full flex flex-col overflow-hidden bg-background">
      {/* Filters Strip */}
      <div className="flex items-center justify-between p-4 border-b border-border shrink-0">
        <div className="flex items-center gap-4 flex-wrap">
          {/* Timeframe tabs */}
          <div className="flex bg-muted p-1 rounded-md">
            {["ALL", "15m", "1h", "4h", "1d"].map(tf => (
              <button
                key={tf}
                onClick={() => setTimeframe(tf)}
                className={`px-3 py-1 text-xs font-bold rounded-sm transition-all ${
                  timeframe === tf
                    ? "bg-background text-foreground shadow-sm"
                    : "text-muted-foreground hover:text-foreground"
                }`}
              >
                {tf}
              </button>
            ))}
          </div>

          <div className="h-6 w-px bg-border" />

          {/* Direction filter */}
          <div className="flex gap-2">
            {["ALL", "BUY", "SELL"].map(d => (
              <button
                key={d}
                onClick={() => setFilter(d)}
                className={`px-3 py-1 text-xs font-semibold rounded border transition-colors flex items-center gap-1 ${
                  filter === d && d === "BUY"  ? "bg-signal-buy/20 border-signal-buy text-signal-buy" :
                  filter === d && d === "SELL" ? "bg-signal-sell/20 border-signal-sell text-signal-sell" :
                  filter === d                 ? "bg-accent border-accent text-accent-foreground" :
                  d === "BUY"  ? "border-border text-muted-foreground hover:border-signal-buy hover:text-signal-buy" :
                  d === "SELL" ? "border-border text-muted-foreground hover:border-signal-sell hover:text-signal-sell" :
                  "border-border text-muted-foreground hover:border-accent"
                }`}
              >
                {d === "BUY"  && <ArrowUpRight className="h-3 w-3" />}
                {d === "SELL" && <ArrowDownRight className="h-3 w-3" />}
                {d}
              </button>
            ))}
          </div>

          <div className="h-6 w-px bg-border" />

          {/* Trade Type filter */}
          <div className="flex gap-2">
            {[
              { label: "Both", value: "ALL" },
              { label: "Intraday", value: "INTRADAY" },
              { label: "Swing", value: "SWING" },
              { label: "BTST", value: "BTST" }
            ].map(d => (
              <button
                key={d.value}
                onClick={() => setTradeTypeFilter(d.value)}
                className={`px-3 py-1 text-xs font-semibold rounded border transition-colors flex items-center gap-1 ${
                  tradeTypeFilter === d.value
                    ? "bg-accent border-accent text-accent-foreground"
                    : "border-border text-muted-foreground hover:border-accent"
                }`}
              >
                {d.label}
              </button>
            ))}
          </div>

          {/* Signal count */}
          {!isLoading && (
            <span className="text-xs text-muted-foreground font-mono">
              {signals.length} signal{signals.length !== 1 ? "s" : ""}
            </span>
          )}
        </div>

        <Button
          variant="outline"
          size="sm"
          onClick={() => triggerScan.mutate(undefined)}
          disabled={stats?.scanning || triggerScan.isPending}
          className="text-xs h-8"
        >
          <RefreshCw className={`h-3 w-3 mr-2 ${stats?.scanning ? "animate-spin" : ""}`} />
          Force Scan
        </Button>
      </div>

      {/* Market Breadth Widget */}
      {(stats as any)?.market_breadth && (
        <div className="flex items-center gap-2 p-2 mx-4 mt-4 bg-muted/30 border border-border rounded-lg text-sm shrink-0">
          <div className="font-semibold px-2">NIFTY 100 Breadth:</div>
          <div className="flex-1 h-2 bg-muted rounded-full overflow-hidden flex">
            <div 
              className="h-full bg-signal-buy transition-all" 
              style={{ width: `${(stats as any).market_breadth.bullish_pct}%` }} 
            />
            <div 
              className="h-full bg-signal-sell transition-all" 
              style={{ width: `${(stats as any).market_breadth.bearish_pct}%` }} 
            />
          </div>
          <div className="text-xs font-mono text-signal-buy px-2">{(stats as any).market_breadth.bullish_pct}% Bullish</div>
          <div className="text-xs font-mono text-signal-sell px-2">{(stats as any).market_breadth.bearish_pct}% Bearish</div>
        </div>
      )}

      {/* Table */}
      <div className="flex-1 overflow-auto p-4">
        {isLoading && !signalsData ? (
          <div className="h-full flex flex-col items-center justify-center text-muted-foreground">
            <Activity className="h-8 w-8 animate-pulse text-primary mb-4" />
            <p className="font-mono text-sm">Initial scan in progress...</p>
            <p className="text-xs opacity-70 mt-2">Scanning 50 Nifty symbols × 4 timeframes.</p>
          </div>
        ) : signals.length === 0 ? (
          <div className="h-full flex flex-col items-center justify-center text-muted-foreground bg-card/50 rounded-lg border border-border border-dashed">
            <BarChart2 className="h-8 w-8 mb-4 opacity-50" />
            <p className="font-mono text-sm">No signals found</p>
            <p className="text-xs opacity-70 mt-1">Try adjusting filters or wait for next scan.</p>
          </div>
        ) : (
          <div className="rounded-md border border-border bg-card overflow-hidden">
            <table className="w-full text-sm text-left">
              <thead className="text-xs text-muted-foreground bg-muted/50 border-b border-border sticky top-0 uppercase tracking-wider z-10">
                <tr>
                  <Th label="Symbol" />
                  <Th label="TF / Type" />
                  <Th label="Dir" />
                  <Th label="Score" col="score" />
                  <Th label="CMP" col="close" className="text-right" />
                  <Th label="Entry (Time)" col="signal_time" className="text-right" />
                  <Th label="SL1" />
                  <Th label="TP1" />
                  <Th label="TP2" />
                  <Th label="Day Chg" col="daily_move_pct" className="text-right" />
                  <Th label="Regime" />
                  <Th label="Setup" />
                  <Th label="RSI" col="rsi" className="text-right" />
                  <Th label="ADX" col="adx" className="text-right" />
                  <Th label="State" />
                  <Th label="P&L" col="pnl_pct" className="text-right" />
                  <Th label="ETA" col="eta_hrs" className="text-right" />
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {signals.map((row: Signal & Record<string, unknown>, i) => {
                  const iSwing = (row.intraday_or_swing as string) === "Swing";
                  const dailyMove = row.daily_move_pct as number | null | undefined;
                  const etaHrs = row.eta_hrs as number | null | undefined;

                  const moodInfo = getRegimeMoodInfo(row);

                  return (
                    <tr
                      key={`${row.symbol}-${row.timeframe}-${i}`}
                      className={`transition-colors cursor-pointer group ${moodInfo.rowClass} ${moodInfo.borderClass}`}
                      onClick={() => setLocation(`/chart/${row.symbol}/${row.timeframe}`)}
                    >
                      {/* Symbol */}
                      <td className="px-4 py-2.5">
                        <div className="flex flex-col gap-0.5">
                          <span className="flex items-center gap-1.5 font-bold font-mono">
                            {row.symbol}
                            {(row as any).transition === "BTST" && (
                              <span className="bg-yellow-500/20 text-yellow-500 text-[9px] font-bold px-1 rounded uppercase tracking-wider">BTST</span>
                            )}
                          </span>
                          <div className="flex items-center gap-1.5 text-[9px] font-normal tracking-wide">
                            <span className="text-muted-foreground">{(row as any).sector || "NSE"}</span>
                            {(row as any).relative_volume && (row as any).relative_volume > 1.5 && (
                              <span className="text-orange-400 font-medium whitespace-nowrap">🔥 {(row as any).relative_volume.toFixed(1)}x Vol</span>
                            )}
                          </div>
                        </div>
                      </td>

                      {/* TF + Intraday/Swing */}
                      <td className="px-4 py-2.5">
                        <div className="flex flex-col gap-0.5">
                          <span className="font-mono text-xs text-muted-foreground">{row.timeframe}</span>
                          <span className={`text-[9px] font-bold uppercase tracking-wider ${iSwing ? "text-purple-400" : "text-cyan-400"}`}>
                            {iSwing ? "Swing" : "Intra"}
                          </span>
                        </div>
                      </td>

                      {/* Direction */}
                      <td className="px-4 py-2.5">
                        <div className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-bold tracking-widest ${
                          row.direction === "BUY"
                            ? "bg-signal-buy/20 text-signal-buy border border-signal-buy/30"
                            : "bg-signal-sell/20 text-signal-sell border border-signal-sell/30"
                        }`}>
                          {row.direction === "BUY"  && <ArrowUpRight className="h-3 w-3" />}
                          {row.direction === "SELL" && <ArrowDownRight className="h-3 w-3" />}
                          {row.direction}
                        </div>
                      </td>

                      {/* Score & Bias */}
                      <td className="px-4 py-2.5">
                        <div className="flex flex-col gap-1">
                          <div className="flex items-center gap-2">
                            <span className="font-mono text-xs w-7 tabular-nums">{row.score.toFixed(0)}</span>
                            <div className="w-14 h-1.5 bg-muted rounded-full overflow-hidden">
                              <div
                                className={`h-full rounded-full ${
                                  row.score >= 70 ? "bg-signal-buy" :
                                  row.score >= 50 ? "bg-yellow-500" : "bg-signal-sell"
                                }`}
                                style={{ width: `${Math.min(row.score, 100)}%` }}
                              />
                            </div>
                          </div>
                          <span className={`text-[9px] uppercase font-bold tracking-wider ${
                            row.bias?.includes("BULL") ? "text-signal-buy" :
                            row.bias?.includes("BEAR") ? "text-signal-sell" : "text-muted-foreground"
                          }`}>
                            {row.bias || "—"}
                          </span>
                        </div>
                      </td>

                      {/* CMP */}
                      <td className="px-4 py-2.5 font-mono text-right font-medium tabular-nums">
                        {row.close.toFixed(2)}
                      </td>

                      {/* Entry (Time) */}
                      <td className="px-4 py-2.5 text-right flex flex-col items-end gap-0.5">
                        <span className="font-mono text-xs tabular-nums text-foreground">
                          {row.entry_price != null && Number(row.entry_price) > 0 ? Number(row.entry_price).toFixed(2) : "—"}
                        </span>
                        <span className="font-mono text-[10px] text-muted-foreground">
                          {formatSignalTime(row.signal_time)}
                        </span>
                      </td>

                      {/* SL1 */}
                      <td className="px-4 py-2.5 font-mono text-xs text-signal-sell/80 tabular-nums">
                        <div className="flex flex-col gap-0.5">
                          <span>{row.sl1 != null ? row.sl1.toFixed(2) : "—"}</span>
                          {(row as any).sl_distance_pct != null && (
                            <span className="text-[10px] text-red-400/80">(-{(row as any).sl_distance_pct.toFixed(1)}%)</span>
                          )}
                        </div>
                      </td>

                      {/* TP1 */}
                      <td className="px-4 py-2.5 font-mono text-xs text-signal-buy/80 tabular-nums">
                        {row.tp1 != null ? row.tp1.toFixed(2) : "—"}
                      </td>

                      {/* TP2 */}
                      <td className="px-4 py-2.5 font-mono text-xs text-signal-buy/60 tabular-nums">
                        {row.tp2 != null ? row.tp2.toFixed(2) : "—"}
                      </td>

                      {/* Day Change */}
                      <td className="px-4 py-2.5 font-mono text-right text-xs tabular-nums">
                        {dailyMove != null ? (
                          <span className={dailyMove > 0 ? "text-signal-buy" : dailyMove < 0 ? "text-signal-sell" : "text-muted-foreground"}>
                            {dailyMove > 0 ? "+" : ""}{dailyMove.toFixed(2)}%
                          </span>
                        ) : <span className="text-muted-foreground">—</span>}
                      </td>

                      {/* Regime MTF */}
                      <td className="px-4 py-2.5">
                        <div className="flex flex-col gap-1 items-start">
                          <div className="flex gap-1 items-center pb-[1px]">
                            <RegimeBox value={row.regime_15m as string} label="15m" activeStatus={(row.active_timeframes as Record<string, string>)?.[`15m`]} />
                            <RegimeBox value={row.regime_1h as string} label="1h" activeStatus={(row.active_timeframes as Record<string, string>)?.[`1h`]} />
                            <RegimeBox value={row.regime_4h as string} label="4h" activeStatus={(row.active_timeframes as Record<string, string>)?.[`4h`]} />
                            <RegimeBox value={row.regime_1d as string} label="1d" activeStatus={(row.active_timeframes as Record<string, string>)?.[`1d`]} />
                          </div>
                          <span className={`text-[9px] px-1.5 py-0.5 rounded border uppercase tracking-wider font-bold ${moodInfo.badgeClass}`}>
                            {moodInfo.label}
                          </span>
                        </div>
                      </td>

                      {/* Setup */}
                      <td className="px-4 py-2.5">
                        <div className="flex flex-col gap-0.5 items-start">
                          <span className="px-1.5 py-0.5 bg-accent text-accent-foreground text-[10px] font-mono rounded whitespace-nowrap">
                            {row.setup || "—"}
                          </span>
                          {(row as any).win_rate_pct != null && (
                            <span className="text-[9px] text-muted-foreground font-semibold">{(row as any).win_rate_pct.toFixed(0)}% WR</span>
                          )}
                        </div>
                      </td>

                      {/* RSI */}
                      <td className="px-4 py-2.5 font-mono text-xs text-right tabular-nums">
                        {row.rsi != null ? (
                          <div className="flex items-center justify-end gap-1.5">
                            {row.rsi > 70 && <span className="px-1 py-0.5 bg-signal-sell/20 text-signal-sell border border-signal-sell/40 rounded text-[9px] font-bold uppercase">OB</span>}
                            {row.rsi < 30 && <span className="px-1 py-0.5 bg-signal-buy/20 text-signal-buy border border-signal-buy/40 rounded text-[9px] font-bold uppercase">OS</span>}
                            <span className={`px-2 py-0.5 rounded border font-semibold ${moodInfo.badgeClass}`}>
                              {row.rsi.toFixed(1)}
                            </span>
                          </div>
                        ) : <span className="text-muted-foreground">—</span>}
                      </td>

                      {/* ADX */}
                      <td className="px-4 py-2.5 font-mono text-xs text-right tabular-nums">
                        {row.adx != null ? (
                          <div className="flex items-center justify-end gap-1.5">
                            {row.adx >= 25 && <span className="px-1 py-0.5 bg-foreground/10 text-foreground text-[9px] font-bold rounded uppercase">STR</span>}
                            <span className={`px-2 py-0.5 rounded border font-semibold ${moodInfo.badgeClass}`}>
                              {row.adx.toFixed(1)}
                            </span>
                          </div>
                        ) : <span className="text-muted-foreground">—</span>}
                      </td>

                      {/* State */}
                      <td className="px-4 py-2.5">
                        <div className="flex items-center gap-1.5">
                          <div className={`h-2 w-2 rounded-full flex-shrink-0 ${
                            row.state === "ACTIVE"  ? "bg-signal-buy shadow-[0_0_6px_var(--color-signal-buy)] animate-pulse" :
                            row.state === "PENDING" ? "bg-yellow-500" : "bg-muted-foreground"
                          }`} />
                          <span className="text-xs font-semibold text-muted-foreground tracking-wider">
                            {row.state}
                          </span>
                        </div>
                      </td>

                      {/* P&L */}
                      <td className="px-4 py-2.5 text-right font-mono text-xs font-medium tabular-nums">
                        {row.state === "ACTIVE" && (row as any).live_pnl_pct != null ? (
                          <div className="flex flex-col items-end">
                            <span className={(row as any).live_pnl_pct > 0 ? "text-signal-buy" : (row as any).live_pnl_pct < 0 ? "text-signal-sell" : "text-muted-foreground"}>
                              {(row as any).live_pnl_abs > 0 ? "+" : ""}{(row as any).live_pnl_abs?.toFixed(2)} ({(row as any).live_pnl_pct > 0 ? "+" : ""}{(row as any).live_pnl_pct?.toFixed(2)}%)
                            </span>
                          </div>
                        ) : row.pnl_pct != null ? (
                          <span className={row.pnl_pct > 0 ? "text-signal-buy" : row.pnl_pct < 0 ? "text-signal-sell" : "text-muted-foreground"}>
                            {row.pnl_pct > 0 ? "+" : ""}{row.pnl_pct.toFixed(2)}%
                          </span>
                        ) : <span className="text-muted-foreground">-</span>}
                      </td>

                      {/* ETA */}
                      <td className="px-4 py-2.5 text-right font-mono text-xs text-muted-foreground tabular-nums">
                        {etaHrs != null ? (
                          <span className={etaHrs < 0 ? "text-signal-sell" : etaHrs < 2 ? "text-yellow-400" : ""}>
                            {etaHrs > 0 ? `${etaHrs.toFixed(0)}h` : `+${Math.abs(etaHrs).toFixed(0)}h OD`}
                          </span>
                        ) : <span>—</span>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
