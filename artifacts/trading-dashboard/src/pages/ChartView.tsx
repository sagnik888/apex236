import { useEffect, useRef, useState } from "react";
import { useParams, Link, useLocation } from "wouter";
import {
  createChart,
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  createSeriesMarkers,
  CrosshairMode,
  LineStyle,
  TickMarkType,
} from "lightweight-charts";
import type { IChartApi, Time, SeriesMarker, PriceLineOptions } from "lightweight-charts";
import { useGetChart, customFetch } from "@workspace/api-client-react";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowLeft, Activity, Target, Shield, ArrowRight,
  ArrowUpRight, ArrowDownRight, TrendingUp, TrendingDown, Clock, Zap, Layers,
  History as HistoryIcon,
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

const IST = "Asia/Kolkata";

/**
 * Format a chart timestamp in IST.
 *
 * The backend emits a true UTC epoch (`pd.Timestamp(ts).timestamp()`), and
 * lightweight-charts renders UTCTimestamp values in UTC unless told otherwise.
 * Without these formatters an NSE session rendered on the time axis as
 * 03:45–10:00 instead of 09:15–15:30, while the tooltip helpers above already
 * converted to IST — so the axis and the tooltip disagreed by 5h30m and every
 * visual review of a losing trade was read against the wrong clock.
 *
 * These convert for DISPLAY only. Never add 19800 to the epoch itself: shifting
 * the data would desynchronise the crosshair, the signal markers and any
 * comparison against backend timestamps, trading a visible bug for a silent one.
 */
function istTick(unixSec: number, kind: TickMarkType): string {
  const d = new Date(unixSec * 1000);
  switch (kind) {
    case TickMarkType.Year:
      return d.toLocaleDateString("en-IN", { year: "numeric", timeZone: IST });
    case TickMarkType.Month:
      return d.toLocaleDateString("en-IN", { month: "short", year: "2-digit", timeZone: IST });
    case TickMarkType.DayOfMonth:
      return d.toLocaleDateString("en-IN", { day: "2-digit", month: "short", timeZone: IST });
    default:
      return d.toLocaleTimeString("en-IN", {
        hour: "2-digit", minute: "2-digit", hour12: false, timeZone: IST,
      });
  }
}

function istCrosshair(unixSec: number): string {
  return new Date(unixSec * 1000).toLocaleString("en-IN", {
    day: "2-digit", month: "short",
    hour: "2-digit", minute: "2-digit", hour12: false,
    timeZone: IST,
  });
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function addPriceLine(series: any, price: number | null | undefined, color: string, title: string, style = LineStyle.Dashed, width: 1 | 2 = 1) {
  if (price == null || price <= 0) return null;
  return series.createPriceLine({ price, color, lineWidth: width, lineStyle: style, axisLabelVisible: true, title } as PriceLineOptions);
}

// ── Trade History Helpers ─────────────────────────────────────────────────────

type ClosedTrade = {
  id: number;
  symbol: string;
  timeframe: string;
  direction: string;
  trade_type: string | null;
  entry_time: string | null;
  entry_price: number | null;
  exit_time: string | null;
  exit_price: number | null;
  sl1: number | null;
  tp1: number | null;
  exit_reason: string;
  pnl_pct: number | null;
};

function fmtTenure(entryIso: string | null, exitIso: string | null): string {
  if (!entryIso || !exitIso) return "—";
  try {
    const ms = new Date(exitIso).getTime() - new Date(entryIso).getTime();
    if (isNaN(ms) || ms < 0) return "—";
    const hrs = ms / (1000 * 60 * 60);
    if (hrs < 1) return `${Math.round(hrs * 60)}m`;
    if (hrs < 24) return `${hrs.toFixed(1)}h`;
    return `${(hrs / 24).toFixed(1)}d`;
  } catch { return "—"; }
}

function exitReasonBadge(reason: string): { label: string; cls: string } {
  const r = reason.toUpperCase();
  if (r.includes("SL2") || r.includes("MAX LOSS")) return { label: reason, cls: "bg-red-600/20 text-red-300 border-red-600/40" };
  if (r.includes("SL")) return { label: reason, cls: "bg-signal-sell/15 text-signal-sell border-signal-sell/30" };
  if (r.includes("T1") || r.includes("T2") || r.includes("T3") || r.includes("TARGET") || r.includes("BOOKED")) return { label: reason, cls: "bg-signal-buy/15 text-signal-buy border-signal-buy/30" };
  if (r.includes("TSL")) return { label: reason, cls: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30" };
  if (r.includes("REPAINT")) return { label: reason, cls: "bg-amber-500/15 text-amber-300 border-amber-500/30" };
  if (r.includes("MOMENTUM")) return { label: reason, cls: "bg-blue-500/15 text-blue-300 border-blue-500/30" };
  if (r.includes("EOD") || r.includes("SESSION")) return { label: reason, cls: "bg-purple-500/15 text-purple-300 border-purple-500/30" };
  return { label: reason || "—", cls: "bg-muted/30 text-muted-foreground border-border" };
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function ChartView() {
  const { symbol, timeframe } = useParams<{ symbol: string; timeframe: string }>();
  const [_, setLocation] = useLocation();
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const chartRef    = useRef<IChartApi | null>(null);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const seriesRef   = useRef<any>(null);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const markersRef  = useRef<any>(null);
  const priceLinesRef = useRef<any[]>([]);
  // MACD sub-chart refs
  const macdContainerRef = useRef<HTMLDivElement>(null);
  const macdChartRef = useRef<IChartApi | null>(null);
  const macdHistRef = useRef<any>(null);
  const macdLineRef = useRef<any>(null);
  const macdSignalRef = useRef<any>(null);
  const [chartReady, setChartReady] = useState(false);
  const [macdHeader, setMacdHeader] = useState({ macd: 0, signal: 0, hist: 0 });

  const safeSymbol    = symbol    || "NIFTY";
  const safeTimeframe = timeframe || "15m";

  const { data: chartData, isLoading, error } = useGetChart(safeSymbol, safeTimeframe, {
    query: {
      // A cache miss now starts a background chart warm-up. Poll briefly until
      // candles arrive, then return to the normal low-frequency refresh.
      // 2.5s keeps the cold-load poll under the API's 60/min chart rate limit.
      refetchInterval: (query) => {
        const data = query.state.data;
        return !data || data.candles.length === 0 ? 2500 : 30000;
      },
      queryKey: ["/api/chart", safeSymbol, safeTimeframe],
    },
  });

  // Fetch closed trades for this symbol from the history API
  const { data: historyData } = useQuery({
    queryKey: ["/api/history", safeSymbol],
    queryFn: () => customFetch<{ trades: ClosedTrade[]; total: number }>(`/api/history?symbol=${encodeURIComponent(safeSymbol)}&limit=50`),
    refetchInterval: 30000,
  });
  const closedTrades = historyData?.trades ?? [];

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
        // NSE trades 09:15-15:30 IST. Without this the axis reads 03:45-10:00.
        tickMarkFormatter: (time: Time, tickMarkType: TickMarkType) =>
          istTick(time as number, tickMarkType),
      },
      localization: {
        // Crosshair / tooltip clock, kept consistent with the axis above.
        timeFormatter: (time: Time) => istCrosshair(time as number),
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

  // ── Create MACD sub-chart ─────────────────────────────────────────────────
  useEffect(() => {
    if (!macdContainerRef.current) return;

    const macdChart = createChart(macdContainerRef.current, {
      layout: {
        background: { color: "#090E17" },
        textColor:  "#94A3B8",
        fontFamily: "monospace",
      },
      grid: {
        vertLines: { color: "#1E293B" },
        horzLines: { color: "#1E293B44" },
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
        tickMarkFormatter: (time: Time, tickMarkType: TickMarkType) =>
          istTick(time as number, tickMarkType),
      },
      localization: {
        timeFormatter: (time: Time) => istCrosshair(time as number),
      },
      rightPriceScale: { borderColor: "#1E293B" },
      autoSize: true,
    });

    const histSeries = macdChart.addSeries(HistogramSeries, {
      priceLineVisible: false,
      lastValueVisible: false,
      priceFormat: { type: "price", precision: 4, minMove: 0.0001 },
    });
    const macdLine = macdChart.addSeries(LineSeries, {
      color: "#2962FF",
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: false,
      priceFormat: { type: "price", precision: 4, minMove: 0.0001 },
    });
    const signalLine = macdChart.addSeries(LineSeries, {
      color: "#FF6D00",
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
      priceFormat: { type: "price", precision: 4, minMove: 0.0001 },
    });

    macdChartRef.current = macdChart;
    macdHistRef.current = histSeries;
    macdLineRef.current = macdLine;
    macdSignalRef.current = signalLine;

    const onResize = () => macdChart.applyOptions({ autoSize: true });
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      macdChart.remove();
      macdChartRef.current = null;
      macdHistRef.current = null;
      macdLineRef.current = null;
      macdSignalRef.current = null;
    };
  }, []);

  // ── Crosshair + TimeScale sync between main and MACD charts ───────────────
  useEffect(() => {
    const mainChart = chartRef.current;
    const macdChart = macdChartRef.current;
    if (!mainChart || !macdChart) return;

    let syncingCrosshair = false;
    let syncingRange = false;

    const onMainCrosshair = (param: any) => {
      if (syncingCrosshair) return;
      syncingCrosshair = true;
      try {
        if (param.time && macdHistRef.current) {
          macdChart.setCrosshairPosition(NaN, param.time, macdHistRef.current);
        } else {
          macdChart.clearCrosshairPosition();
        }
      } catch { /* chart may be disposed */ }
      syncingCrosshair = false;
    };
    const onMacdCrosshair = (param: any) => {
      if (syncingCrosshair) return;
      syncingCrosshair = true;
      try {
        if (param.time && seriesRef.current) {
          mainChart.setCrosshairPosition(NaN, param.time, seriesRef.current);
          // Update MACD header values from crosshair position
          const histVal = param.seriesData?.get(macdHistRef.current);
          const macdVal = param.seriesData?.get(macdLineRef.current);
          const sigVal = param.seriesData?.get(macdSignalRef.current);
          if (macdVal) {
            setMacdHeader({
              macd: macdVal.value ?? 0,
              signal: sigVal?.value ?? 0,
              hist: histVal?.value ?? 0,
            });
          }
        } else {
          mainChart.clearCrosshairPosition();
        }
      } catch { /* chart may be disposed */ }
      syncingCrosshair = false;
    };
    const onMainRange = (range: any) => {
      if (syncingRange || !range) return;
      syncingRange = true;
      try { macdChart.timeScale().setVisibleLogicalRange(range); } catch {}
      syncingRange = false;
    };
    const onMacdRange = (range: any) => {
      if (syncingRange || !range) return;
      syncingRange = true;
      try { mainChart.timeScale().setVisibleLogicalRange(range); } catch {}
      syncingRange = false;
    };

    mainChart.subscribeCrosshairMove(onMainCrosshair);
    macdChart.subscribeCrosshairMove(onMacdCrosshair);
    mainChart.timeScale().subscribeVisibleLogicalRangeChange(onMainRange);
    macdChart.timeScale().subscribeVisibleLogicalRangeChange(onMacdRange);

    return () => {
      try { mainChart.unsubscribeCrosshairMove(onMainCrosshair); } catch {}
      try { macdChart.unsubscribeCrosshairMove(onMacdCrosshair); } catch {}
      try { mainChart.timeScale().unsubscribeVisibleLogicalRangeChange(onMainRange); } catch {}
      try { macdChart.timeScale().unsubscribeVisibleLogicalRangeChange(onMacdRange); } catch {}
    };
  }, [chartReady]);

  // ── Update when chartData changes ──────────────────────────────────────────
  useEffect(() => {
    const series = seriesRef.current;
    const chart  = chartRef.current;
    if (!series || !chart || !chartData || !chartReady) return;

    // Candles — API already sends Unix seconds (UTC)
    const seen = new Set<number>();
    const candles = chartData.candles
      .map(c => ({ time: c.time as Time, open: c.open, high: c.high, low: c.low, close: c.close }))
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
          time:     s.time as Time,
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
    priceLinesRef.current.forEach(pl => {
      try { series.removePriceLine(pl); } catch {}
    });
    priceLinesRef.current = [];

    if (chartData.active_trade) {
      const t = chartData.active_trade;
      const lines = [
        addPriceLine(series, t.entry_price, "#F8FAFC", "ENTRY", LineStyle.Dashed, 2),
        addPriceLine(series, t.sl1,         "#FF3366", "SL1",   LineStyle.Dashed),
        addPriceLine(series, t.sl2,         "#FF6688", "SL2",   LineStyle.Dotted),
        addPriceLine(series, t.tsl,         "#FF9900", "TSL",   LineStyle.LargeDashed),
        addPriceLine(series, t.tp1, "#00FF66", t.t1_hit ? "T1 ✓" : "T1", t.t1_hit ? LineStyle.Dotted : LineStyle.Dashed),
        addPriceLine(series, t.tp2, "#00DD55", t.t2_hit ? "T2 ✓" : "T2", t.t2_hit ? LineStyle.Dotted : LineStyle.Dashed),
        addPriceLine(series, t.tp3, "#00BB44", t.t3_hit ? "T3 ✓" : "T3", t.t3_hit ? LineStyle.Dotted : LineStyle.Dashed)
      ].filter(Boolean);
      priceLinesRef.current = lines;
    }

    try { chart.timeScale().fitContent(); } catch { /* */ }

    // ── Update MACD sub-chart data ──────────────────────────────────────────
    const macdChart = macdChartRef.current;
    const chartDataAny = chartData as any;
    if (macdChart && chartDataAny?.macd?.length) {
      const macdArr = chartDataAny.macd as { time: number; macd: number; signal: number; histogram: number }[];
      const seenMacd = new Set<number>();
      const sorted = macdArr
        .filter(m => { if (seenMacd.has(m.time)) return false; seenMacd.add(m.time); return true; })
        .sort((a, b) => a.time - b.time);

      // Histogram (green when >= 0, red when < 0)
      try {
        macdHistRef.current?.setData(
          sorted.map(m => ({
            time: m.time as Time,
            value: m.histogram,
            color: m.histogram >= 0 ? "#26A69A" : "#EF5350",
          }))
        );
      } catch (e) { console.error("MACD hist:", e); }

      // MACD line
      try {
        macdLineRef.current?.setData(
          sorted.map(m => ({ time: m.time as Time, value: m.macd }))
        );
      } catch (e) { console.error("MACD line:", e); }

      // Signal line
      try {
        macdSignalRef.current?.setData(
          sorted.map(m => ({ time: m.time as Time, value: m.signal }))
        );
      } catch (e) { console.error("MACD signal:", e); }

      // Update header with latest values
      const last = sorted[sorted.length - 1];
      if (last) setMacdHeader({ macd: last.macd, signal: last.signal, hist: last.histogram });

      try { macdChart.timeScale().fitContent(); } catch {}
    }
  }, [chartData, chartReady]);

  // ── Live P&L ───────────────────────────────────────────────────────────────
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const at = chartData?.active_trade as any;
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
          
          {/* Multi-Timeframe Switcher Buttons */}
          <div className="flex items-center gap-1 bg-muted/60 p-1 rounded-md border border-border/50">
            {["5m", "15m", "1h", "4h", "1d"].map((tf) => (
              <button
                key={tf}
                onClick={() => setLocation(`/chart/${encodeURIComponent(safeSymbol)}/${tf}`)}
                className={`px-2 py-0.5 rounded text-xs font-bold font-mono transition-all ${
                  safeTimeframe === tf
                    ? "bg-primary text-primary-foreground shadow-sm"
                    : "text-muted-foreground hover:text-foreground hover:bg-muted"
                }`}
              >
                {tf}
              </button>
            ))}
          </div>

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
        <div className="flex-1 flex flex-col bg-background">
          {/* Main candlestick chart — 75% */}
          <div className="relative" style={{ flex: "3 1 0%" }}>
            <div className="absolute inset-0" ref={chartContainerRef} />

            {(isLoading || (!error && chartData?.candles.length === 0)) && (
              <div className="absolute inset-0 z-10 flex items-center justify-center text-muted-foreground text-sm font-mono bg-background/80 backdrop-blur-sm">
                {isLoading ? "Loading chart data..." : "Preparing chart data..."}
              </div>
            )}

            {error && (
              <div className="absolute inset-0 z-10 flex items-center justify-center text-destructive text-sm font-mono bg-background/80 backdrop-blur-sm">
                Failed to load chart
              </div>
            )}
          </div>

          {/* MACD sub-chart — 25% */}
          <div className="relative border-t border-border" style={{ flex: "1 1 0%" }}>
            {/* MACD header overlay */}
            <div className="absolute top-1 left-2 z-10 flex items-center gap-2 text-[11px] font-mono pointer-events-none">
              <span className="text-muted-foreground font-bold">MACD</span>
              <span className="text-muted-foreground">12 26 close 9</span>
              <span className="text-[#2962FF] font-bold">{macdHeader.macd.toFixed(2)}</span>
              <span className="text-[#FF6D00] font-bold">{macdHeader.signal.toFixed(2)}</span>
              <span className={`font-bold ${macdHeader.hist >= 0 ? "text-[#26A69A]" : "text-[#EF5350]"}`}>{macdHeader.hist >= 0 ? "+" : ""}{macdHeader.hist.toFixed(2)}</span>
            </div>
            <div className="absolute inset-0" ref={macdContainerRef} />
          </div>
        </div>

        {/* Side Panel */}
        <div className="w-72 border-l border-border bg-card flex flex-col shrink-0 overflow-auto">

          {/* ── Active Trade & Options Block ── */}
          {at ? (
            <div className="p-4 border-b border-border space-y-4">
              <div>
                <h3 className="font-bold text-xs uppercase tracking-wider mb-2 text-muted-foreground">Active Trade (Cash / Futures)</h3>

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
                    <span className="text-xs text-muted-foreground uppercase font-semibold">Underlying Entry</span>
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

              {/* ── Options Card ── */}
              {(at.option_type || at.option_strike) && (
                <div className="bg-amber-500/10 border border-amber-500/40 rounded-lg p-3 space-y-3">
                  <div className="flex items-center justify-between border-b border-amber-500/20 pb-2">
                    <span className="text-xs font-bold text-amber-500 uppercase tracking-wider flex items-center gap-1">
                      <Zap className="h-3.5 w-3.5" /> ATM Option Guidance
                    </span>
                    <span className="px-1.5 py-0.5 bg-amber-500/20 text-amber-400 font-mono text-[10px] rounded font-bold">
                      {at.option_type || (direction === "BUY" ? "CE" : "PE")}
                    </span>
                  </div>

                  <div className="text-xs font-mono font-bold text-foreground break-all">
                    {at.option_symbol || `${safeSymbol} ${at.option_strike || ""} ${at.option_type || ""}`}
                  </div>

                  {at.option_strike && (
                    <div className="flex justify-between items-center text-xs">
                      <span className="text-muted-foreground">Strike Price:</span>
                      <span className="font-mono font-bold text-amber-400">₹{at.option_strike}</span>
                    </div>
                  )}

                  <div className="bg-background/80 rounded border border-border/60 p-2.5 space-y-1.5 font-mono text-xs">
                    <div className="flex justify-between items-center">
                      <span className="text-muted-foreground">Opt Entry:</span>
                      <span className="font-bold tabular-nums">₹{at.option_entry?.toFixed(2) ?? "—"}</span>
                    </div>
                    <div className="flex justify-between items-center text-signal-sell">
                      <span>Opt SL1:</span>
                      <span className="font-bold tabular-nums">₹{at.option_sl1?.toFixed(2) ?? "—"}</span>
                    </div>
                    {at.option_sl2 != null && (
                      <div className="flex justify-between items-center text-signal-sell/70">
                        <span>Opt SL2:</span>
                        <span className="tabular-nums">₹{at.option_sl2.toFixed(2)}</span>
                      </div>
                    )}
                    {at.option_tsl != null && (
                      <div className="flex justify-between items-center text-orange-400">
                        <span>Opt TSL:</span>
                        <span className="font-bold tabular-nums">₹{at.option_tsl.toFixed(2)}</span>
                      </div>
                    )}
                  </div>

                  <div className="bg-background/80 rounded border border-border/60 p-2.5 space-y-1 font-mono text-xs">
                    <div className="text-[10px] text-muted-foreground uppercase font-semibold mb-1">Option Targets</div>
                    {[
                      { label: "Opt T1", price: at.option_tp1 },
                      { label: "Opt T2", price: at.option_tp2 },
                      { label: "Opt T3", price: at.option_tp3 },
                    ].map((otp, idx) => otp.price != null ? (
                      <div key={idx} className="flex justify-between items-center">
                        <span className="text-muted-foreground">{otp.label}:</span>
                        <span className="font-bold tabular-nums text-signal-buy">₹{otp.price.toFixed(2)}</span>
                      </div>
                    ) : null)}
                  </div>

                  <div className="text-[10px] font-mono text-muted-foreground border-t border-amber-500/20 pt-2 flex items-center justify-between">
                    <span>Execution Broker:</span>
                    <span className="text-foreground font-semibold">
                      {(at.option_type === "CE" || direction === "BUY") ? "Upstox API (v2)" : "AngelOne API"}
                    </span>
                  </div>
                </div>
              )}
            </div>
          ) : (
            <div className="p-4 border-b border-border text-center text-muted-foreground">
              <Target className="h-5 w-5 mx-auto mb-2 opacity-40" />
              <p className="text-xs">No active trade</p>
            </div>
          )}

          {/* ── Other Timeframes ── */}
          {chartData?.other_active_trades && Object.keys(chartData.other_active_trades).length > 0 && (
            <div className="p-4 border-b border-border bg-card/40">
              <h3 className="font-bold text-xs uppercase tracking-wider mb-3 text-muted-foreground flex items-center gap-1.5">
                <Layers className="h-3.5 w-3.5" /> Other Timeframes
              </h3>
              <div className="space-y-2">
                {Object.entries(chartData.other_active_trades).map(([tf, oat]: [string, any]) => {
                  const oatDir = oat.direction === "LONG" ? "BUY" : oat.direction === "SHORT" ? "SELL" : oat.direction;
                  const oatPnlPct = oat.entry_price && currentPrice
                    ? ((currentPrice - oat.entry_price) / oat.entry_price * 100) * (oatDir === "BUY" ? 1 : -1)
                    : null;
                  
                  return (
                    <div key={tf} className="border border-border rounded bg-background p-2.5 space-y-2 relative overflow-hidden">
                      <div className={`absolute left-0 top-0 bottom-0 w-1 ${oatDir === "BUY" ? "bg-signal-buy" : "bg-signal-sell"}`} />
                      
                      <div className="flex items-center justify-between pl-2">
                        <div className="flex items-center gap-2">
                          <span className="px-1.5 py-0.5 bg-muted text-muted-foreground font-mono text-[10px] rounded font-bold">
                            {tf}
                          </span>
                          <span className={`text-[11px] font-bold tracking-wider ${oatDir === "BUY" ? "text-signal-buy" : "text-signal-sell"}`}>
                            {oatDir}
                          </span>
                        </div>
                        {oatPnlPct != null && (
                          <span className={`text-xs font-mono font-bold ${oatPnlPct >= 0 ? "text-signal-buy" : "text-signal-sell"}`}>
                            {oatPnlPct >= 0 ? "+" : ""}{oatPnlPct.toFixed(2)}%
                          </span>
                        )}
                      </div>

                      <div className="flex items-center justify-between pl-2 text-[11px] font-mono">
                        <div className="text-muted-foreground">
                          EP: <span className="text-foreground font-bold tabular-nums">₹{oat.entry_price?.toFixed(2) ?? "—"}</span>
                        </div>
                        <div className="flex items-center gap-2 text-xs">
                          {oat.sl1 != null && <span className="text-signal-sell/80">SL: ₹{oat.sl1.toFixed(1)}</span>}
                          {oat.tp1 != null && <span className="text-signal-buy/80">T1: ₹{oat.tp1.toFixed(1)}</span>}
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {/* ── Signal History (enriched with trade outcomes) ── */}
          <div className="p-4 flex-1">
            <div className="flex items-center justify-between mb-3">
              <h3 className="font-bold text-xs uppercase tracking-wider text-muted-foreground flex items-center gap-1.5">
                <HistoryIcon className="h-3.5 w-3.5" />
                Signal History ({chartData?.signals?.length ?? 0})
              </h3>
              <Link href="/history" className="text-[10px] text-primary hover:text-primary/80 font-medium flex items-center gap-0.5">
                All Trades <ArrowRight className="h-2.5 w-2.5" />
              </Link>
            </div>
            <div className="space-y-2">
              {chartData?.signals?.slice().reverse().slice(0, 10).map((sig) => {
                const sigDir = sig.type;
                const sigTimeMs = (sig.time as number) * 1000;
                let matchedTrade: ClosedTrade | null = null;
                let bestDiff = Infinity;
                for (const t of closedTrades) {
                  const tDir = t.direction === "LONG" ? "BUY" : t.direction === "SHORT" ? "SELL" : t.direction;
                  if (tDir !== sigDir || !t.entry_time) continue;
                  const diff = Math.abs(new Date(t.entry_time).getTime() - sigTimeMs);
                  if (diff < 48 * 3600000 && diff < bestDiff) { bestDiff = diff; matchedTrade = t; }
                }
                const pnl = matchedTrade?.pnl_pct ?? null;
                const isWin = pnl != null && pnl > 0;
                const isLoss = pnl != null && pnl < 0;
                const badge = matchedTrade ? exitReasonBadge(matchedTrade.exit_reason) : null;
                const tenure = matchedTrade ? fmtTenure(matchedTrade.entry_time, matchedTrade.exit_time) : null;

                return (
                  <div key={sig.time} className={`p-2.5 rounded border overflow-hidden relative ${
                    isWin ? "bg-signal-buy/[0.04] border-signal-buy/20" :
                    isLoss ? "bg-signal-sell/[0.04] border-signal-sell/20" :
                    "bg-background border-border"
                  }`}>
                    <div className={`absolute left-0 top-0 bottom-0 w-1 ${
                      isWin ? "bg-signal-buy" : isLoss ? "bg-signal-sell" : sig.type === "BUY" ? "bg-signal-buy/40" : "bg-signal-sell/40"
                    }`} />
                    <div className="pl-2">
                      <div className="flex items-center justify-between">
                        <div className="flex items-center gap-1.5">
                          <span className={`text-xs font-bold ${sig.type === "BUY" ? "text-signal-buy" : "text-signal-sell"}`}>
                            {sig.type === "BUY" ? "↗" : "↘"} {sig.type}
                          </span>
                          <span className="font-mono text-xs font-medium tabular-nums">₹{sig.price > 0 ? sig.price.toFixed(2) : "—"}</span>
                        </div>
                        {pnl != null ? (
                          <span className={`font-mono text-xs font-bold tabular-nums ${isWin ? "text-signal-buy" : isLoss ? "text-signal-sell" : "text-muted-foreground"}`}>
                            {pnl >= 0 ? "+" : ""}{pnl.toFixed(2)}%
                          </span>
                        ) : (
                          <span className="text-[9px] text-muted-foreground italic">pending</span>
                        )}
                      </div>
                      <div className="flex items-center justify-between mt-0.5">
                        <span className="text-[10px] text-muted-foreground font-mono">
                          Bar: {fmtDateOnly(sig.time)} {fmtTimeOnly(sig.time)}
                        </span>
                        <div className="flex items-center gap-1.5">
                          <span className="text-[10px] text-muted-foreground">{sig.setup || "—"}</span>
                          {sig.score != null && sig.score > 0 && (
                            <span className="text-[10px] text-muted-foreground font-mono">score {sig.score.toFixed(0)}</span>
                          )}
                        </div>
                      </div>
                      <div className="flex gap-2 mt-0.5 text-[10px] font-mono">
                        {sig.sl != null && <span className="text-signal-sell/70">SL ₹{sig.sl.toFixed(2)}</span>}
                        {sig.tp1 != null && <span className="text-signal-buy/70">T1 ₹{sig.tp1.toFixed(2)}</span>}
                      </div>
                      {matchedTrade && (
                        <div className="mt-1.5 pt-1.5 border-t border-border/50">
                          <div className="flex items-center gap-1.5 text-[11px] font-mono mb-0.5">
                            <span className="text-muted-foreground">₹{matchedTrade.entry_price?.toFixed(2) ?? "—"}</span>
                            <ArrowRight className="h-2.5 w-2.5 text-muted-foreground/50 flex-shrink-0" />
                            <span className={isWin ? "text-signal-buy font-bold" : isLoss ? "text-signal-sell font-bold" : "text-foreground"}>
                              ₹{matchedTrade.exit_price?.toFixed(2) ?? "—"}
                            </span>
                          </div>
                          <div className="flex items-center justify-between">
                            <div className="flex items-center gap-1.5">
                              {badge && <span className={`rounded border px-1 py-0.5 text-[9px] font-bold ${badge.cls}`}>{badge.label}</span>}
                              {tenure && (
                                <span className="text-[9px] text-muted-foreground font-mono flex items-center gap-0.5">
                                  <Clock className="h-2.5 w-2.5" />{tenure}
                                </span>
                              )}
                            </div>
                            <span className="text-[9px] text-muted-foreground font-mono">
                              {matchedTrade.exit_time ? fmtDateTime(matchedTrade.exit_time) : "—"}
                            </span>
                          </div>
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}
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
