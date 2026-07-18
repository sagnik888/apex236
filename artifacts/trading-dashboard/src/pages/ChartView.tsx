import { useEffect, useRef, useState } from "react";
import { useParams, Link } from "wouter";
import {
  createChart,
  CandlestickSeries,
  createSeriesMarkers,
  CrosshairMode,
  LineStyle,
} from "lightweight-charts";
import type { IChartApi, Time, SeriesMarker, PriceLineOptions } from "lightweight-charts";
import { useGetChart } from "@workspace/api-client-react";
import {
  ArrowLeft, Activity, Target, Shield,
  ArrowUpRight, ArrowDownRight, TrendingUp, TrendingDown, Clock,
} from "lucide-react";

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleString("en-IN", {
      day: "2-digit", month: "short",
      hour: "2-digit", minute: "2-digit", hour12: false,
      timeZone: "Asia/Kolkata",
    });
  } catch { return iso; }
}

function fmtTimeOnly(unixSec: number): string {
  return new Date(unixSec * 1000).toLocaleTimeString("en-IN", {
    hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Kolkata",
  });
}

function fmtDateOnly(unixSec: number): string {
  return new Date(unixSec * 1000).toLocaleDateString("en-IN", {
    day: "2-digit", month: "short", timeZone: "Asia/Kolkata",
  });
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function addPriceLine(series: any, price: number | null | undefined, color: string, title: string, style = LineStyle.Dashed, width: 1 | 2 = 1) {
  if (price == null || price <= 0) return;
  series.createPriceLine({ price, color, lineWidth: width, lineStyle: style, axisLabelVisible: true, title } as PriceLineOptions);
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function ChartView() {
  const { symbol, timeframe } = useParams<{ symbol: string; timeframe: string }>();
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const chartRef    = useRef<IChartApi | null>(null);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const seriesRef   = useRef<any>(null);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const markersRef  = useRef<any>(null);
  const [chartReady, setChartReady] = useState(false);

  const safeSymbol    = symbol    || "NIFTY";
  const safeTimeframe = timeframe || "15m";

  const { data: chartData, isLoading, error } = useGetChart(safeSymbol, safeTimeframe, {
    query: { refetchInterval: 30000, queryKey: ["/api/chart", safeSymbol, safeTimeframe] },
  });

  // ── Create chart once ──────────────────────────────────────────────────────
  useEffect(() => {
    if (!chartContainerRef.current) return;

    const chart = createChart(chartContainerRef.current, {
      layout: {
        background: { color: "#090E17" },
        textColor:  "#94A3B8",
        fontFamily: "monospace",
      },
      grid: {
        vertLines: { color: "#1E293B" },
        horzLines: { color: "#1E293B" },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { width: 1, color: "#475569", style: LineStyle.Dotted },
        horzLine: { width: 1, color: "#475569", style: LineStyle.Dotted },
      },
      timeScale: {
        borderColor: "#1E293B",
        timeVisible: true,
        secondsVisible: false,
      },
      rightPriceScale: { borderColor: "#1E293B" },
      autoSize: true,
    });

    const series = chart.addSeries(CandlestickSeries, {
      upColor:       "#00E5FF",
      downColor:     "#FF3366",
      borderVisible: false,
      wickUpColor:   "#00E5FF",
      wickDownColor: "#FF3366",
    });

    chartRef.current  = chart;
    seriesRef.current = series;
    setChartReady(true);

    const onResize = () => chart.applyOptions({ autoSize: true });
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      chart.remove();
      chartRef.current   = null;
      seriesRef.current  = null;
      markersRef.current = null;
      setChartReady(false);
    };
  }, []);

  // ── Update when chartData changes ──────────────────────────────────────────
  useEffect(() => {
    const series = seriesRef.current;
    const chart  = chartRef.current;
    if (!series || !chart || !chartData || !chartReady) return;

    // ── Apply IST Offset (5h 30m) so chart displays correctly in IST ──
    const IST_OFFSET = 19800; // 5.5 * 60 * 60 seconds

    // Candles — API already sends Unix seconds (UTC)
    const seen = new Set<number>();
    const candles = chartData.candles
      .map(c => ({ time: (c.time + IST_OFFSET) as Time, open: c.open, high: c.high, low: c.low, close: c.close }))
      .filter(c => { const t = c.time as number; if (seen.has(t)) return false; seen.add(t); return true; })
      .sort((a, b) => (a.time as number) - (b.time as number));

    try { series.setData(candles); } catch (e) { console.error("setData:", e); }

    // Detach previous markers
    if (markersRef.current) {
      try { series.detachPrimitive(markersRef.current); } catch { /* */ }
      markersRef.current = null;
    }

    // Signal markers — price is now correctly set in backend
    if (chartData.signals?.length) {
      const seenMarkerTimes = new Set<number>();
      const markers: SeriesMarker<Time>[] = chartData.signals
        .map(s => ({
          time:     (s.time + IST_OFFSET) as Time,
          position: s.type === "BUY" ? "belowBar" as const : "aboveBar" as const,
          color:    s.type === "BUY" ? "#00FF66" : "#FF3366",
          shape:    s.type === "BUY" ? "arrowUp"  as const : "arrowDown" as const,
          text:     `${s.type}${s.score != null ? " " + Number(s.score).toFixed(0) : ""}`,
          size:     2,
        }))
        .filter(m => {
          const t = m.time as number;
          if (!seen.has(t)) return false; // Must exist in candles data!
          if (seenMarkerTimes.has(t)) return false; // Must be strictly unique!
          seenMarkerTimes.add(t);
          return true;
        })
        .sort((a, b) => (a.time as number) - (b.time as number));

      try { markersRef.current = createSeriesMarkers(series, markers); } catch (e) { console.error("markers:", e); }
    }

    // Price lines for active trade (setData wipes them, so redraw after)
    if (chartData.active_trade) {
      const t = chartData.active_trade;
      addPriceLine(series, t.entry_price, "#F8FAFC", "ENTRY", LineStyle.Dashed, 2);
      addPriceLine(series, t.sl1,         "#FF3366", "SL1",   LineStyle.Dashed);
      addPriceLine(series, t.sl2,         "#FF6688", "SL2",   LineStyle.Dotted);
      addPriceLine(series, t.tsl,         "#FF9900", "TSL",   LineStyle.LargeDashed);
      addPriceLine(series, t.tp1, "#00FF66", t.t1_hit ? "T1 ✓" : "T1", t.t1_hit ? LineStyle.Dotted : LineStyle.Dashed);
      addPriceLine(series, t.tp2, "#00DD55", t.t2_hit ? "T2 ✓" : "T2", t.t2_hit ? LineStyle.Dotted : LineStyle.Dashed);
      addPriceLine(series, t.tp3, "#00BB44", t.t3_hit ? "T3 ✓" : "T3", t.t3_hit ? LineStyle.Dotted : LineStyle.Dashed);
    }

    try { chart.timeScale().fitContent(); } catch { /* */ }
  }, [chartData, chartReady]);

  // ── Live P&L ───────────────────────────────────────────────────────────────
  const at = chartData?.active_trade;
  const currentPrice = chartData?.current_price;
  const direction = at?.direction === "LONG" ? "BUY" : at?.direction === "SHORT" ? "SELL" : at?.direction;
  const livePnlPct = at && at.entry_price && currentPrice
    ? ((currentPrice - at.entry_price) / at.entry_price * 100) * (direction === "BUY" ? 1 : -1)
    : null;

  return (
    <div className="flex flex-col h-full bg-background overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between p-3 border-b border-border shrink-0 bg-card">
        <div className="flex items-center gap-3">
          <Link href="/" className="p-1.5 bg-muted hover:bg-muted/80 text-muted-foreground hover:text-foreground rounded transition-colors">
            <ArrowLeft className="h-4 w-4" />
          </Link>
          <h1 className="text-xl font-bold font-mono tracking-tight">{safeSymbol}</h1>
          <span className="px-2 py-0.5 bg-muted rounded text-xs font-bold font-mono text-muted-foreground">{safeTimeframe}</span>
          {chartData?.scan_run_at && (
            <span className="text-[10px] text-muted-foreground font-mono hidden md:block">
              Data as of {fmtDateTime(chartData.scan_run_at)}
            </span>
          )}
        </div>

        <div className="flex items-center gap-6">
          {currentPrice != null && (
            <div className="flex flex-col items-end">
              <span className="text-[10px] uppercase text-muted-foreground font-semibold">CMP</span>
              <span className="font-mono text-xl font-bold tabular-nums">₹{currentPrice.toFixed(2)}</span>
            </div>
          )}
          {livePnlPct != null && (
            <div className="flex flex-col items-end">
              <span className="text-[10px] uppercase text-muted-foreground font-semibold">Live P&L</span>
              <div className={`flex items-center gap-1 font-mono text-sm font-bold ${livePnlPct >= 0 ? "text-signal-buy" : "text-signal-sell"}`}>
                {livePnlPct >= 0 ? <TrendingUp className="h-3 w-3" /> : <TrendingDown className="h-3 w-3" />}
                {livePnlPct > 0 ? "+" : ""}{livePnlPct.toFixed(2)}%
              </div>
            </div>
          )}
        </div>
      </div>

      <div className="flex-1 flex overflow-hidden relative">
        <div className="flex-1 relative bg-background">
          <div className="absolute inset-0" ref={chartContainerRef} />
          
          {isLoading && (
            <div className="absolute inset-0 z-10 flex items-center justify-center text-muted-foreground text-sm font-mono bg-background/80 backdrop-blur-sm">
              Loading chart data...
            </div>
          )}
          
          {error && (
            <div className="absolute inset-0 z-10 flex items-center justify-center text-destructive text-sm font-mono bg-background/80 backdrop-blur-sm">
              Failed to load chart
            </div>
          )}
        </div>

        {/* Side Panel */}
        <div className="w-72 border-l border-border bg-card flex flex-col shrink-0 overflow-auto">

          {/* ── Active Trade Block ── */}
          {at ? (
            <div className="p-4 border-b border-border">
              <h3 className="font-bold text-xs uppercase tracking-wider mb-3 text-muted-foreground">Active Trade</h3>

              {/* Direction badge */}
              <div className={`inline-flex items-center gap-1.5 px-3 py-1 rounded text-sm font-bold tracking-widest mb-3 ${
                direction === "BUY"
                  ? "bg-signal-buy/20 text-signal-buy border border-signal-buy/30"
                  : "bg-signal-sell/20 text-signal-sell border border-signal-sell/30"
              }`}>
                {direction === "BUY" ? <ArrowUpRight className="h-4 w-4" /> : <ArrowDownRight className="h-4 w-4" />}
                {direction}
              </div>

              {/* Times */}
              <div className="space-y-1 mb-3 text-[11px] font-mono text-muted-foreground">
                {at.signal_time && (
                  <div className="flex items-center gap-1.5">
                    <Clock className="h-3 w-3 flex-shrink-0" />
                    <span className="text-foreground/70 font-semibold">Signal:</span>
                    <span>{fmtDateTime(at.signal_time)}</span>
                  </div>
                )}
                {at.entry_time && (
                  <div className="flex items-center gap-1.5">
                    <Clock className="h-3 w-3 flex-shrink-0" />
                    <span className="text-foreground/70 font-semibold">Entry:</span>
                    <span>{fmtDateTime(at.entry_time)}</span>
                  </div>
                )}
              </div>

              {/* Entry / Stops */}
              <div className="bg-background rounded border border-border p-3 mb-3 space-y-2">
                <div className="flex justify-between items-center">
                  <span className="text-xs text-muted-foreground uppercase font-semibold">Entry</span>
                  <span className="font-mono font-bold tabular-nums">₹{at.entry_price?.toFixed(2) ?? "—"}</span>
                </div>
                <div className="flex justify-between items-center text-signal-sell">
                  <span className="text-xs uppercase font-semibold flex items-center gap-1">
                    <Shield className="h-3 w-3" /> SL1
                  </span>
                  <span className="font-mono font-bold tabular-nums">₹{at.sl1?.toFixed(2) ?? "—"}</span>
                </div>
                {at.sl2 != null && (
                  <div className="flex justify-between items-center text-signal-sell/60">
                    <span className="text-xs uppercase font-semibold flex items-center gap-1">
                      <Shield className="h-3 w-3" /> SL2
                    </span>
                    <span className="font-mono tabular-nums">₹{at.sl2.toFixed(2)}</span>
                  </div>
                )}
                {at.tsl != null && (
                  <div className="flex justify-between items-center text-orange-400">
                    <span className="text-xs uppercase font-semibold flex items-center gap-1">
                      <Shield className="h-3 w-3" /> TSL
                    </span>
                    <span className="font-mono font-bold tabular-nums">₹{at.tsl.toFixed(2)}</span>
                  </div>
                )}
              </div>

              {/* Targets */}
              <div className="bg-background rounded border border-border p-3 space-y-2">
                <span className="text-xs text-muted-foreground uppercase font-semibold flex items-center gap-1 mb-2">
                  <Target className="h-3 w-3" /> Targets
                </span>
                {[
                  { label: "T1", price: at.tp1, hit: at.t1_hit },
                  { label: "T2", price: at.tp2, hit: at.t2_hit },
                  { label: "T3", price: at.tp3, hit: at.t3_hit },
                ].map((tp, i) => tp.price != null ? (
                  <div key={i} className="flex justify-between items-center">
                    <span className={`text-xs font-mono ${tp.hit ? "text-signal-buy" : "text-muted-foreground"}`}>{tp.label}</span>
                    <div className="flex items-center gap-2">
                      <span className={`font-mono text-sm tabular-nums ${tp.hit ? "text-signal-buy/60 line-through" : "font-bold"}`}>
                        ₹{tp.price.toFixed(2)}
                      </span>
                      {tp.hit && <span className="w-2 h-2 rounded-full bg-signal-buy shadow-[0_0_5px_var(--color-signal-buy)]" />}
                    </div>
                  </div>
                ) : null)}
              </div>
            </div>
          ) : (
            <div className="p-4 border-b border-border text-center text-muted-foreground">
              <Target className="h-5 w-5 mx-auto mb-2 opacity-40" />
              <p className="text-xs">No active trade</p>
            </div>
          )}

          {/* ── Signal History ── */}
          <div className="p-4 flex-1">
            <h3 className="font-bold text-xs uppercase tracking-wider mb-3 text-muted-foreground">
              Signal History ({chartData?.signals?.length ?? 0})
            </h3>
            <div className="space-y-2">
              {chartData?.signals?.slice().reverse().slice(0, 12).map((sig, i) => (
                <div key={i} className="flex items-start gap-2 p-2 rounded bg-background border border-border">
                  <div className={`w-1.5 h-1.5 rounded-full flex-shrink-0 mt-1 ${sig.type === "BUY" ? "bg-signal-buy" : "bg-signal-sell"}`} />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center justify-between">
                      <span className={`text-xs font-bold ${sig.type === "BUY" ? "text-signal-buy" : "text-signal-sell"}`}>
                        {sig.type}
                      </span>
                      <span className="font-mono text-xs font-medium tabular-nums">₹{sig.price > 0 ? sig.price.toFixed(2) : "—"}</span>
                    </div>
                    {/* Bar time — when signal candle closed */}
                    <div className="text-[10px] text-muted-foreground font-mono mt-0.5">
                      Bar: {fmtDateOnly(sig.time)} {fmtTimeOnly(sig.time)}
                    </div>
                    <div className="flex items-center justify-between mt-0.5">
                      <span className="text-[10px] text-muted-foreground">{sig.setup || "—"}</span>
                      {sig.score != null && sig.score > 0 && (
                        <span className="text-[10px] text-muted-foreground font-mono">score {sig.score.toFixed(0)}</span>
                      )}
                    </div>
                    {/* SL / TP summary */}
                    <div className="flex gap-2 mt-0.5 text-[10px] font-mono">
                      {sig.sl != null && <span className="text-signal-sell/70">SL ₹{sig.sl.toFixed(2)}</span>}
                      {sig.tp1 != null && <span className="text-signal-buy/70">T1 ₹{sig.tp1.toFixed(2)}</span>}
                    </div>
                  </div>
                </div>
              ))}
              {(!chartData?.signals || chartData.signals.length === 0) && (
                <div className="text-xs text-muted-foreground text-center py-4 italic">No signals on this chart.</div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
