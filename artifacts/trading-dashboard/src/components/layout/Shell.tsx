import { useEffect, useState } from "react";
import { Link, useLocation } from "wouter";
import { Activity, LayoutDashboard, PieChart, Circle, BarChart3, History, Settings } from "lucide-react";
import { useGetScannerStats, useGetSession } from "@workspace/api-client-react";
import { useWebSocket } from "@/hooks/use-websocket";
import { NotificationBell, NotificationToasts } from "@/components/notifications/NotificationCenter";
import { SystemHealthPanel } from "@/components/layout/SystemHealthPanel";

// Scan interval by session (seconds) — mirrors backend logic
function expectedScanInterval(sessionStatus: string): number {
  if (sessionStatus === "OPEN")     return 60;    // live 15m refresh
  if (sessionStatus === "PRE_OPEN") return 60;    // do not miss 09:15 transition
  return 3600;                                    // 1 hour
}

function useIstClock(): string {
  const [time, setTime] = useState(() =>
    new Date().toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone: "Asia/Kolkata" })
  );
  useEffect(() => {
    const id = setInterval(() => {
      setTime(new Date().toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone: "Asia/Kolkata" }));
    }, 1000);
    return () => clearInterval(id);
  }, []);
  return time;
}

function useScanCountdown(lastScan: string | null | undefined, sessionStatus: string): string {
  const [label, setLabel] = useState("—");
  useEffect(() => {
    if (!lastScan) { setLabel("—"); return; }
    const interval = expectedScanInterval(sessionStatus);
    const tick = () => {
      const elapsed = (Date.now() - new Date(lastScan).getTime()) / 1000;
      const remaining = Math.max(0, interval - elapsed);
      const m = Math.floor(remaining / 60);
      const s = Math.floor(remaining % 60);
      setLabel(`${m}:${s.toString().padStart(2, "0")}`);
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [lastScan, sessionStatus]);
  return label;
}

export function Shell({ children }: { children: React.ReactNode }) {
  const [location] = useLocation();
  useWebSocket();

  const statsQuery = useGetScannerStats({
    query: { refetchInterval: 15000, queryKey: ["/api/stats"] }
  });
  const stats = statsQuery.data;
  const { data: session } = useGetSession({
    query: { refetchInterval: 60000, queryKey: ["/api/session"] }
  });

  const marketOpen    = stats?.market_open     ?? session?.market_open     ?? false;
  const sessionStatus = stats?.session_status  ?? session?.session_status  ?? "CLOSED";
  const istClock      = useIstClock();
  const countdown     = useScanCountdown(stats?.last_scan ?? null, sessionStatus);

  return (
    <div className="flex h-[100dvh] w-full flex-col bg-background text-foreground overflow-hidden">
      {/* Top Navbar */}
      <header className="flex h-14 items-center justify-between gap-2 border-b border-border bg-card px-2 sm:px-4 shrink-0">
        <div className="flex min-w-0 items-center gap-2 lg:gap-6">
          <div className="flex shrink-0 items-center gap-2 text-primary font-bold tracking-tight">
            <Activity className="h-5 w-5" />
            <span className="hidden sm:block">APEX NSE 236</span>
            <span className="sm:hidden">APEX</span>
          </div>

          <nav className="flex items-center gap-1">
            <Link
              href="/"
              className={`flex items-center gap-2 px-3 py-1.5 text-sm font-medium rounded-md transition-colors ${
                location === "/" ? "bg-primary/10 text-primary" : "text-muted-foreground hover:bg-muted hover:text-foreground"
              }`}
            >
              <LayoutDashboard className="h-4 w-4" />
              <span className="hidden lg:block">Scanner</span>
            </Link>
            <Link
              href="/trades"
              className={`flex items-center gap-2 px-3 py-1.5 text-sm font-medium rounded-md transition-colors ${
                location === "/trades" ? "bg-primary/10 text-primary" : "text-muted-foreground hover:bg-muted hover:text-foreground"
              }`}
            >
              <PieChart className="h-4 w-4" />
              <span className="hidden lg:block">Active Trades</span>
            </Link>
            <Link
              href="/analytics"
              className={`flex items-center gap-2 px-3 py-1.5 text-sm font-medium rounded-md transition-colors ${
                location === "/analytics" ? "bg-primary/10 text-primary" : "text-muted-foreground hover:bg-muted hover:text-foreground"
              }`}
            >
              <BarChart3 className="h-4 w-4" />
              <span className="hidden lg:block">Performance</span>
            </Link>
            <Link
              href="/history"
              className={`flex items-center gap-2 px-3 py-1.5 text-sm font-medium rounded-md transition-colors ${
                location === "/history" ? "bg-primary/10 text-primary" : "text-muted-foreground hover:bg-muted hover:text-foreground"
              }`}
            >
              <History className="h-4 w-4" />
              <span className="hidden lg:block">History</span>
            </Link>
            <Link
              href="/settings"
              className={`flex items-center gap-2 px-3 py-1.5 text-sm font-medium rounded-md transition-colors ${
                location === "/settings" ? "bg-primary/10 text-primary" : "text-muted-foreground hover:bg-muted hover:text-foreground"
              }`}
            >
              <Settings className="h-4 w-4" />
              <span className="hidden lg:block">Settings</span>
            </Link>
          </nav>
        </div>

        <div className="flex shrink-0 items-center gap-1.5 lg:gap-2 text-sm">
          {/* NSE Market Session Badge */}
          <div className={`flex items-center gap-1.5 px-2.5 py-1 rounded border text-xs font-bold tracking-wider
            ${marketOpen
              ? "border-signal-buy/40 bg-signal-buy/10 text-signal-buy"
              : sessionStatus === "PRE_OPEN"
                ? "border-yellow-500/40 bg-yellow-500/10 text-yellow-400"
                : "border-border bg-muted/30 text-muted-foreground"
            }`}
          >
            <Circle className={`h-2 w-2 fill-current ${marketOpen ? "animate-pulse" : ""}`} />
            <span className="hidden sm:block">NSE</span> {sessionStatus.replace("_", "-")}
          </div>

          {/* IST Clock — ticks every second client-side */}
          <span className="text-muted-foreground text-xs font-mono hidden min-[1360px]:block tabular-nums">
            {istClock} IST
          </span>

          <SystemHealthPanel
            stats={stats}
            isOffline={statsQuery.isError || statsQuery.failureCount > 0}
            responseUpdatedAt={statsQuery.dataUpdatedAt}
            nextScan={countdown}
          />

          {/* Active / Signals counter */}
          <div className="hidden sm:flex items-center gap-2 px-2 lg:px-3 py-1 border border-border rounded bg-muted/30">
            <div className="flex flex-col items-center">
              <span className="text-[9px] uppercase text-muted-foreground font-semibold leading-none">Active</span>
              <span className="font-mono text-sm leading-tight text-foreground">{stats?.active_trades ?? 0}</span>
            </div>
            <div className="w-px h-5 bg-border" />
            <div className="flex flex-col items-center">
              <span className="text-[9px] uppercase text-muted-foreground font-semibold leading-none">Sigs</span>
              <span className="font-mono text-sm leading-tight">
                <span className="text-signal-buy">{stats?.buy_signals ?? 0}B</span>
                <span className="text-muted-foreground mx-0.5">/</span>
                <span className="text-signal-sell">{stats?.sell_signals ?? 0}S</span>
              </span>
            </div>
          </div>

          {/* Notification bell */}
          <NotificationBell />
        </div>
      </header>

      {/* Main Content */}
      <main className="flex-1 overflow-hidden relative">
        {children}
      </main>

      {/* Toast overlay (bottom-right, above everything) */}
      <NotificationToasts />
    </div>
  );
}
