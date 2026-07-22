import { useEffect, useId, useState } from "react";
import type { StatsResponse } from "@workspace/api-client-react";

type HealthState = "live" | "scanning" | "degraded" | "offline";

interface SystemHealthPanelProps {
  stats?: StatsResponse;
  isOffline: boolean;
  responseUpdatedAt: number;
  nextScan: string;
}

const HEALTH_STYLES: Record<HealthState, { label: string; color: string; text: string; dot: string }> = {
  live: {
    label: "LIVE",
    color: "border-signal-buy/30 bg-signal-buy/[0.08] text-signal-buy",
    text: "text-signal-buy",
    dot: "bg-signal-buy shadow-[0_0_7px_rgba(16,185,129,0.7)]",
  },
  scanning: {
    label: "SCANNING",
    color: "border-primary/30 bg-primary/[0.08] text-primary",
    text: "text-primary",
    dot: "bg-primary shadow-[0_0_7px_hsl(var(--primary)/0.55)] animate-pulse",
  },
  degraded: {
    label: "DEGRADED",
    color: "border-yellow-500/30 bg-yellow-500/[0.08] text-yellow-400",
    text: "text-yellow-400",
    dot: "bg-yellow-400 shadow-[0_0_7px_rgba(250,204,21,0.55)]",
  },
  offline: {
    label: "OFFLINE",
    color: "border-signal-sell/30 bg-signal-sell/[0.08] text-signal-sell",
    text: "text-signal-sell",
    dot: "bg-signal-sell",
  },
};

function formatDuration(milliseconds: number | null | undefined): string {
  if (milliseconds == null || !Number.isFinite(milliseconds)) return "—";
  const ms = Math.max(0, milliseconds);
  if (ms < 1_000) return `${Math.round(ms)}ms`;
  const seconds = ms / 1_000;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = Math.round(seconds % 60);
  return `${minutes}m ${remainingSeconds.toString().padStart(2, "0")}s`;
}

function formatAge(milliseconds: number | null): string {
  if (milliseconds == null || !Number.isFinite(milliseconds)) return "—";
  const seconds = Math.max(0, milliseconds) / 1_000;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  if (seconds < 3_600) return `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`;
  return `${Math.floor(seconds / 3_600)}h ${Math.floor((seconds % 3_600) / 60)}m`;
}

function ageFrom(timestamp: string | null | undefined, now: number): number | null {
  if (!timestamp) return null;
  const parsed = new Date(timestamp).getTime();
  return Number.isFinite(parsed) ? Math.max(0, now - parsed) : null;
}

function Metric({ value, label, title, wide = false }: { value: string; label: string; title: string; wide?: boolean }) {
  return (
    <div
      className={`flex min-w-0 flex-col items-center justify-center leading-none ${wide ? "px-2.5" : "min-w-10 px-2"}`}
      title={title}
      aria-label={`${title}: ${value}`}
    >
      <span className="max-w-full whitespace-nowrap font-mono text-[11px] font-bold tracking-tight text-foreground tabular-nums">
        {value}
      </span>
      <span className="mt-1 text-[9px] font-extrabold uppercase tracking-[0.13em] text-muted-foreground">
        {label}
      </span>
    </div>
  );
}

export function SystemHealthPanel({ stats, isOffline, responseUpdatedAt, nextScan }: SystemHealthPanelProps) {
  const [now, setNow] = useState(Date.now);
  const compactDetailId = useId();

  useEffect(() => {
    const interval = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(interval);
  }, []);

  const health: HealthState = isOffline || !stats
    ? "offline"
    : stats.scanning
      ? "scanning"
      : stats.scan_errors > 0
        ? "degraded"
        : "live";
  const style = HEALTH_STYLES[health];
  const responseAge = responseUpdatedAt > 0 ? Math.max(0, now - responseUpdatedAt) : null;
  const completedScanAge = ageFrom(stats?.last_scan, now);
  const runningAge = stats?.scanning ? ageFrom(stats.scan_started_at, now) : null;
  const freshness = `${formatAge(responseAge)} / scan ${formatAge(completedScanAge)}`;
  const detail = [
    `System status: ${style.label}`,
    `Latest successful stats response: ${formatAge(responseAge)} ago`,
    `Last completed scan: ${formatAge(completedScanAge)} ago`,
    stats?.scanning ? `Current scan elapsed: ${formatAge(runningAge)}` : `Next scheduled scan: ${nextScan}`,
  ].join(". ");

  return (
    <>
      <section
        className="hidden h-10 items-center rounded-lg border border-border/80 bg-background/70 px-1.5 shadow-[inset_0_1px_0_hsl(var(--foreground)/0.025),0_3px_12px_rgba(0,0,0,0.16)] min-[1360px]:flex"
        aria-label="System health telemetry"
        title={detail}
      >
        <div className={`flex h-7 items-center gap-1.5 rounded-md border px-2 text-[10px] font-extrabold tracking-[0.14em] ${style.color}`}>
          <span className={`h-2 w-2 shrink-0 rounded-full ${style.dot}`} aria-hidden="true" />
          <span>{style.label}</span>
        </div>
        <div className="mx-1 h-5 w-px bg-border/80" aria-hidden="true" />
        <Metric value={String(stats?.scan_count ?? 0)} label="Scan" title="Scan cycles started since service launch" />
        <div className="h-5 w-px bg-border/60" aria-hidden="true" />
        <Metric value={formatDuration(stats?.scan_latency_ms)} label="Lat" title="Duration of the last completed full scan" />
        <div className="h-5 w-px bg-border/60" aria-hidden="true" />
        <Metric value={freshness} label="Ref" title={detail} wide />
      </section>

      <section
        className="hidden h-9 items-center gap-2 rounded-md border border-border/80 bg-background/70 px-2 outline-none md:flex min-[1360px]:!hidden focus-visible:border-primary/60 focus-visible:ring-2 focus-visible:ring-primary/20"
        aria-label={`System health: ${style.label}. ${stats?.scan_count ?? 0} scans started.`}
        aria-describedby={compactDetailId}
        title={detail}
        tabIndex={0}
      >
        <span className={`h-2 w-2 shrink-0 rounded-full ${style.dot}`} aria-hidden="true" />
        <span className={`text-[10px] font-extrabold tracking-[0.12em] ${style.text}`}>
          {style.label}
        </span>
        <span className="h-4 w-px bg-border/70" aria-hidden="true" />
        <span className="font-mono text-[11px] font-bold text-foreground tabular-nums">
          {stats?.scan_count ?? 0}
          <span className="ml-1 text-[9px] uppercase tracking-wider text-muted-foreground">scan</span>
        </span>
        <span id={compactDetailId} className="sr-only">{detail}</span>
      </section>
    </>
  );
}
