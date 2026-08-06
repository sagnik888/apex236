import { useQuery } from "@tanstack/react-query";
import { Loader2, History as HistoryIcon } from "lucide-react";
import { customFetch } from "@workspace/api-client-react";

// Closed-trade log. Data comes from the backend DB (/api/history), which is
// outside the generated OpenAPI client, so this page uses fetch directly.

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

async function fetchHistory(): Promise<{ trades: ClosedTrade[]; total: number }> {
  return customFetch("/api/history?limit=500");
}

function fmt(ts: string | null): string {
  if (!ts) return "—";
  const d = new Date(ts);
  if (isNaN(d.getTime())) return ts;
  return d.toLocaleString("en-IN", {
    day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Kolkata",
  });
}

function reasonBadge(reason: string): { label: string; cls: string } {
  const r = reason.toUpperCase();
  if (r.includes("SL2") || r.includes("MAX LOSS")) return { label: reason, cls: "bg-red-600/20 text-red-300 border-red-600/40" };
  if (r.includes("SL")) return { label: reason, cls: "bg-signal-sell/15 text-signal-sell border-signal-sell/30" };
  if (r.includes("T1") || r.includes("T2") || r.includes("T3") || r.includes("TARGET") || r.includes("BOOKED")) return { label: reason, cls: "bg-signal-buy/15 text-signal-buy border-signal-buy/30" };
  if (r.includes("TSL")) return { label: reason, cls: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30" };
  if (r.includes("REPAINT")) return { label: reason, cls: "bg-amber-500/15 text-amber-300 border-amber-500/30" };
  if (r.includes("MOMENTUM")) return { label: reason, cls: "bg-blue-500/15 text-blue-300 border-blue-500/30" };
  return { label: reason || "—", cls: "bg-muted/30 text-muted-foreground border-border" };
}

export default function HistoryPage() {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["/api/history"],
    queryFn: fetchHistory,
    refetchInterval: 30000,
  });

  const trades = data?.trades ?? [];
  const wins = trades.filter((t) => (t.pnl_pct ?? 0) > 0).length;
  const losses = trades.filter((t) => (t.pnl_pct ?? 0) < 0).length;
  const decided = trades.length;
  const wr = decided ? ((wins / decided) * 100).toFixed(1) : "0.0";
  const avgPnl = decided ? trades.reduce((s, t) => s + (t.pnl_pct ?? 0), 0) / decided : 0;
  const avgWin = wins ? trades.filter((t) => (t.pnl_pct ?? 0) > 0).reduce((s, t) => s + (t.pnl_pct ?? 0), 0) / wins : 0;
  const avgLoss = losses ? trades.filter((t) => (t.pnl_pct ?? 0) < 0).reduce((s, t) => s + (t.pnl_pct ?? 0), 0) / losses : 0;
  const profitFactor = avgLoss !== 0 ? Math.abs((avgWin * wins) / (avgLoss * losses)) : 0;

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-6xl px-4 py-6">
        <div className="mb-4 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <HistoryIcon className="h-5 w-5 text-primary" />
            <div>
              <h1 className="text-lg font-bold text-foreground">Trade History</h1>
              <p className="text-xs text-muted-foreground">Closed positions — exits, stop-loss, and target hits. Live positions stay on Active Trades.</p>
            </div>
          </div>
          {!isLoading && !isError && (
            <div className="flex items-center gap-4 text-xs">
              <span className="text-muted-foreground">{trades.length} closed</span>
              <span className="text-muted-foreground">WR <span className="font-mono text-foreground">{wr}%</span></span>
              <span className="text-muted-foreground">Avg <span className={`font-mono ${avgPnl >= 0 ? "text-signal-buy" : "text-signal-sell"}`}>{avgPnl >= 0 ? "+" : ""}{avgPnl.toFixed(2)}%</span></span>
              <span className="text-muted-foreground">W̄ <span className="font-mono text-signal-buy">+{avgWin.toFixed(2)}%</span></span>
              <span className="text-muted-foreground">L̄ <span className="font-mono text-signal-sell">{avgLoss.toFixed(2)}%</span></span>
              <span className="text-muted-foreground">PF <span className={`font-mono ${profitFactor >= 1 ? "text-signal-buy" : "text-signal-sell"}`}>{profitFactor.toFixed(2)}</span></span>
            </div>
          )}
        </div>

        {isLoading ? (
          <div className="flex h-64 items-center justify-center text-muted-foreground"><Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading history…</div>
        ) : isError ? (
          <div className="flex h-64 items-center justify-center text-signal-sell">{(error as Error).message}</div>
        ) : trades.length === 0 ? (
          <div className="flex h-64 flex-col items-center justify-center text-muted-foreground">
            <HistoryIcon className="mb-2 h-8 w-8 opacity-40" />
            <p>No closed trades yet.</p>
            <p className="text-xs">Positions appear here once they exit (SL, target, or momentum).</p>
          </div>
        ) : (
          <div className="overflow-x-auto rounded-lg border border-border">
            <table className="w-full text-sm">
              <thead className="bg-muted/30 text-xs uppercase text-muted-foreground">
                <tr>
                  <th className="px-3 py-2.5 text-left">Symbol</th>
                  <th className="px-3 py-2.5 text-left">TF</th>
                  <th className="px-3 py-2.5 text-left">Type</th>
                  <th className="px-3 py-2.5 text-left">Dir</th>
                  <th className="px-3 py-2.5 text-right">Entry</th>
                  <th className="px-3 py-2.5 text-right">Exit</th>
                  <th className="px-3 py-2.5 text-right">Entry Time</th>
                  <th className="px-3 py-2.5 text-right">Exit Time</th>
                  <th className="px-3 py-2.5 text-left">Reason</th>
                  <th className="px-3 py-2.5 text-right">PnL %</th>
                </tr>
              </thead>
              <tbody>
                {trades.map((t) => {
                  const badge = reasonBadge(t.exit_reason);
                  const pnl = t.pnl_pct ?? 0;
                  return (
                    <tr key={t.id} className="border-t border-border hover:bg-muted/10">
                      <td className="px-3 py-2 font-medium text-foreground">{t.symbol}</td>
                      <td className="px-3 py-2 font-mono text-xs text-muted-foreground">{t.timeframe}</td>
                      <td className="px-3 py-2 text-xs text-muted-foreground">{t.trade_type || "—"}</td>
                      <td className={`px-3 py-2 text-xs font-semibold ${t.direction === "BUY" || t.direction === "LONG" ? "text-signal-buy" : "text-signal-sell"}`}>{t.direction}</td>
                      <td className="px-3 py-2 text-right font-mono text-xs tabular-nums">{t.entry_price?.toFixed(2) ?? "—"}</td>
                      <td className="px-3 py-2 text-right font-mono text-xs tabular-nums">{t.exit_price?.toFixed(2) ?? "—"}</td>
                      <td className="px-3 py-2 text-right font-mono text-[11px] text-muted-foreground tabular-nums">{fmt(t.entry_time)}</td>
                      <td className="px-3 py-2 text-right font-mono text-[11px] text-muted-foreground tabular-nums">{fmt(t.exit_time)}</td>
                      <td className="px-3 py-2"><span className={`rounded border px-1.5 py-0.5 text-[10px] ${badge.cls}`}>{badge.label}</span></td>
                      <td className={`px-3 py-2 text-right font-mono text-xs font-semibold tabular-nums ${pnl > 0 ? "text-signal-buy" : pnl < 0 ? "text-signal-sell" : "text-muted-foreground"}`}>
                        {pnl >= 0 ? "+" : ""}{pnl.toFixed(2)}%
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        <p className="mt-4 text-center text-[11px] text-muted-foreground">
          Simulated strategy exits recorded per scan. Not broker-confirmed fills.
        </p>
      </div>
    </div>
  );
}
