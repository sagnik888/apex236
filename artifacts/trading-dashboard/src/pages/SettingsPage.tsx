import { useEffect, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Save, RotateCcw, Loader2, Check, AlertTriangle } from "lucide-react";
import { customFetch } from "@workspace/api-client-react";
import BrokerAuthPanel from "../components/brokers/BrokerAuthPanel";

// The scanner settings endpoints are outside the generated OpenAPI client,
// so this page talks to /api directly (same-origin via the Vite proxy).

type Settings = {
  enabled_timeframes: string[];
  min_score: number;
  conflict_margin: number;
  min_adx: number;
  use_htf: boolean;
  signal_cooldown: number;
  sl_mode: "auto" | "fixed";
  fixed_sl_pct: number;
  atr_mult: number;
  target_mode: "rr" | "fixed";
  t1_r: number;
  t2_r: number;
  t3_r: number;
  fixed_tp_pct: number;
  exit_at_t1: boolean;
  use_trail: boolean;
  trail_start_r: number;
  trail_mult: number;
  lock_at_t1: boolean;
  exit_confirmation_bars: number;
  max_consecutive_losses: number;
  circuit_pause_bars: number;
  use_session: boolean;
  block_open_noise: boolean;
  block_close_noise: boolean;
  enable_options: boolean;
  strike_mode: string;
  trade_options_intraday: boolean;
  options_broker: string;
  options_stop_mode: string;
  trade_style: "intraday" | "intraday_btst" | "all";
};

const ALL_TFS = ["15m", "1h", "4h", "1d"];
const DEFAULTS: Settings = {
  enabled_timeframes: ["15m", "1h", "4h", "1d"],
  min_score: 60,
  conflict_margin: 20,
  min_adx: 20,
  use_htf: true,
  signal_cooldown: 1,
  sl_mode: "auto",
  fixed_sl_pct: 0.5,
  atr_mult: 1.5,
  target_mode: "rr",
  t1_r: 2.0,
  t2_r: 3.0,
  t3_r: 4.0,
  fixed_tp_pct: 0.75,
  exit_at_t1: false,
  use_trail: true,
  trail_start_r: 1.5,
  trail_mult: 1.8,
  lock_at_t1: true,
  exit_confirmation_bars: 3,
  max_consecutive_losses: 3,
  circuit_pause_bars: 5,
  use_session: true,
  block_open_noise: false,
  block_close_noise: false,
  enable_options: true,
  strike_mode: "Smart Auto",
  trade_options_intraday: true,
  options_broker: "upstox",
  options_stop_mode: "Delta-Translated",
  trade_style: "all",
};

async function fetchSettings(): Promise<Settings> {
  return customFetch("/api/settings");
}

async function saveSettings(body: Settings): Promise<{ settings: Settings }> {
  return customFetch("/api/settings", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

function Card({ title, desc, children }: { title: string; desc?: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-border bg-card p-5">
      <h3 className="text-sm font-semibold text-foreground">{title}</h3>
      {desc && <p className="mt-0.5 text-xs text-muted-foreground">{desc}</p>}
      <div className="mt-4">{children}</div>
    </div>
  );
}

function NumberField({
  label, value, onChange, step = 0.1, min = 0, max = 100, suffix, disabled,
}: {
  label: string; value: number; onChange: (v: number) => void;
  step?: number; min?: number; max?: number; suffix?: string; disabled?: boolean;
}) {
  return (
    <label className={`flex flex-col gap-1 ${disabled ? "opacity-40" : ""}`}>
      <span className="text-xs text-muted-foreground">{label}</span>
      <div className="flex items-center gap-1.5">
        <input
          type="number" value={value} step={step} min={min} max={max} disabled={disabled}
          onChange={(e) => onChange(parseFloat(e.target.value))}
          className="w-24 rounded border border-border bg-background px-2 py-1.5 font-mono text-sm text-foreground focus:border-primary focus:outline-none disabled:cursor-not-allowed"
        />
        {suffix && <span className="text-xs text-muted-foreground">{suffix}</span>}
      </div>
    </label>
  );
}

function Segmented<T extends string>({
  options, value, onChange,
}: { options: { v: T; label: string }[]; value: T; onChange: (v: T) => void }) {
  return (
    <div className="inline-flex rounded-md border border-border bg-background p-0.5">
      {options.map((o) => (
        <button
          key={o.v} onClick={() => onChange(o.v)}
          className={`rounded px-3 py-1.5 text-xs font-medium transition-colors ${
            value === o.v ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-foreground"
          }`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

function Toggle({ checked, onChange, label, desc, disabled = false }: { checked: boolean; onChange: (v: boolean) => void; label: string; desc?: string; disabled?: boolean }) {
  return (
    <button disabled={disabled} onClick={() => onChange(!checked)} className={`flex w-full items-start justify-between gap-4 text-left disabled:cursor-not-allowed ${disabled ? "opacity-40" : ""}`}>
      <span className="flex flex-col">
        <span className="text-sm text-foreground">{label}</span>
        {desc && <span className="text-xs text-muted-foreground">{desc}</span>}
      </span>
      <span className={`mt-0.5 h-5 w-9 shrink-0 rounded-full p-0.5 transition-colors ${checked ? "bg-primary" : "bg-muted"}`}>
        <span className={`block h-4 w-4 rounded-full bg-white transition-transform ${checked ? "translate-x-4" : ""}`} />
      </span>
    </button>
  );
}

export default function SettingsPage() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ["/api/settings"], queryFn: fetchSettings });
  const [form, setForm] = useState<Settings>(DEFAULTS);
  const [savedTick, setSavedTick] = useState(false);

  useEffect(() => { if (data) setForm(data); }, [data]);

  const mutation = useMutation({
    mutationFn: saveSettings,
    onSuccess: (res) => {
      setForm(res.settings);
      qc.invalidateQueries({ queryKey: ["/api/settings"] });
      qc.invalidateQueries({ queryKey: ["/api/signals"] });
      setSavedTick(true);
      setTimeout(() => setSavedTick(false), 2500);
    },
  });

  const set = <K extends keyof Settings>(k: K, v: Settings[K]) => setForm((f) => ({ ...f, [k]: v }));
  const toggleTf = (tf: string) => {
    const has = form.enabled_timeframes.includes(tf);
    const next = has ? form.enabled_timeframes.filter((t) => t !== tf) : [...form.enabled_timeframes, tf];
    if (next.length) set("enabled_timeframes", ALL_TFS.filter((t) => next.includes(t)));
  };

  if (isLoading) {
    return <div className="flex h-full items-center justify-center text-muted-foreground"><Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading settings…</div>;
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-3xl px-4 py-6">
        <div className="mb-6 flex items-center justify-between">
          <div>
            <h1 className="text-lg font-bold text-foreground">Scanner Settings</h1>
            <p className="text-xs text-muted-foreground">Applied from the next scan onward (a rescan is triggered immediately when the scanner is idle).</p>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => setForm(DEFAULTS)}
              className="flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground"
            >
              <RotateCcw className="h-3.5 w-3.5" /> Reset
            </button>
            <button
              onClick={() => mutation.mutate(form)}
              disabled={mutation.isPending}
              className="flex items-center gap-1.5 rounded-md bg-primary px-4 py-1.5 text-xs font-semibold text-primary-foreground hover:opacity-90 disabled:opacity-50"
            >
              {mutation.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : savedTick ? <Check className="h-3.5 w-3.5" /> : <Save className="h-3.5 w-3.5" />}
              {savedTick ? "Saved" : "Save & Apply"}
            </button>
          </div>
        </div>

        {mutation.isError && (
          <div className="mb-4 flex items-center gap-2 rounded-md border border-signal-sell/40 bg-signal-sell/10 px-3 py-2 text-xs text-signal-sell">
            <AlertTriangle className="h-4 w-4" /> {(mutation.error as Error).message}
          </div>
        )}

        <div className="mb-6">
          <BrokerAuthPanel />
        </div>

        <div className="grid gap-4">
          {/* 1. Timeframes */}
          <Card title="Timeframes to scan" desc="Which timeframes the background scanner runs and shows.">
            <div className="flex flex-wrap gap-2">
              {ALL_TFS.map((tf) => {
                const on = form.enabled_timeframes.includes(tf);
                return (
                  <button
                    key={tf} onClick={() => toggleTf(tf)}
                    className={`rounded-md border px-4 py-2 text-sm font-medium transition-colors ${
                      on ? "border-primary bg-primary/10 text-primary" : "border-border text-muted-foreground hover:text-foreground"
                    }`}
                  >
                    {tf}
                  </button>
                );
              })}
            </div>
            <p className="mt-2 text-[11px] text-muted-foreground">
              Trade-type filtering lives on the Scanner tab chips, but you can restrict which signals are taken globally below.
            </p>
          </Card>

          <Card title="Signal Style Limits" desc="Control what types of setups the scanner is allowed to trade. Note: Older active trades of a different style will continue to be managed through their stop losses regardless of this setting.">
            <div className="flex flex-col gap-2">
              <Segmented
                value={form.trade_style}
                onChange={(v) => set("trade_style", v)}
                options={[
                  { v: "intraday", label: "Intraday Only" },
                  { v: "intraday_btst", label: "Intraday + BTST" },
                  { v: "all", label: "Intraday + BTST + Swing (All)" },
                ]}
              />
            </div>
          </Card>

          {/* 2. Stop-loss */}
          <Card title="APEX signal gates" desc="These match the manual APEX Hybrid Pro inputs and apply to every selected timeframe.">
            <div className="flex flex-wrap gap-6">
              <NumberField label="Min confluence score" value={form.min_score} onChange={(v) => set("min_score", v)} step={1} min={0} max={100} />
              <NumberField label="Min bull-bear divergence" value={form.conflict_margin} onChange={(v) => set("conflict_margin", v)} step={1} min={0} max={100} suffix="pts" />
              <NumberField label="Min ADX" value={form.min_adx} onChange={(v) => set("min_adx", v)} step={1} min={0} max={100} />
              <NumberField label="Bars between signals" value={form.signal_cooldown} onChange={(v) => set("signal_cooldown", Math.round(v))} step={1} min={0} max={100} />
            </div>
            <div className="mt-4"><Toggle checked={form.use_htf} onChange={(v) => set("use_htf", v)} label="Require 15m EMA alignment (HTF gate)" /></div>
          </Card>

          <Card title="Stop-loss" desc="How the protective stop on each trade is placed.">
            <div className="flex flex-col gap-4">
              <Segmented
                value={form.sl_mode}
                onChange={(v) => set("sl_mode", v)}
                options={[
                  { v: "auto", label: "Auto (0.5% intraday / ATR swing)" },
                  { v: "fixed", label: "Fixed %" },
                ]}
              />
              <div className="flex flex-wrap gap-6">
                <NumberField label="Fixed SL %" value={form.fixed_sl_pct} onChange={(v) => set("fixed_sl_pct", v)} step={0.1} min={0.1} max={10} suffix="%" disabled={form.sl_mode !== "fixed"} />
                <NumberField label="ATR multiplier" value={form.atr_mult} onChange={(v) => set("atr_mult", v)} step={0.1} min={0.3} max={5} suffix="× ATR" />
              </div>
            </div>
          </Card>

          {/* 3. Targets — includes the user-defined move mode */}
          <Card title="Targets & Reward:Risk" desc="Target mode controls where TP1/TP2/TP3 sit.">
            <div className="flex flex-col gap-4">
              <Segmented
                value={form.target_mode}
                onChange={(v) => set("target_mode", v)}
                options={[
                  { v: "rr", label: "R:R multiples of stop" },
                  { v: "fixed", label: "Fixed % move (your target)" },
                ]}
              />
              {form.target_mode === "rr" ? (
                <div className="flex flex-wrap gap-6">
                  <NumberField label="T1 (R)" value={form.t1_r} onChange={(v) => set("t1_r", v)} step={0.1} min={0.2} max={10} suffix="R" />
                  <NumberField label="T2 (R)" value={form.t2_r} onChange={(v) => set("t2_r", v)} step={0.1} min={0.2} max={15} suffix="R" />
                  <NumberField label="T3 (R)" value={form.t3_r} onChange={(v) => set("t3_r", v)} step={0.1} min={0.2} max={20} suffix="R" />
                </div>
              ) : (
                <div className="rounded-md border border-primary/30 bg-primary/5 p-3">
                  <NumberField label="Target move %" value={form.fixed_tp_pct} onChange={(v) => set("fixed_tp_pct", v)} step={0.05} min={0.1} max={20} suffix="% (T1)" />
                  <p className="mt-2 text-[11px] text-muted-foreground">
                    T1 = entry ± {form.fixed_tp_pct}% · T2 = {(form.fixed_tp_pct * 1.5).toFixed(2)}% · T3 = {(form.fixed_tp_pct * 2).toFixed(2)}%.
                    Enable "Book full position at T1" below to exit the entire trade the moment your target is touched.
                  </p>
                </div>
              )}
            </div>
          </Card>

          {/* 4. Exit behaviour — the user-wish target mode */}
          <Card title="Exit behaviour" desc="How positions are closed once open.">
            <div className="flex flex-col gap-4">
              <Toggle
                checked={form.exit_at_t1}
                onChange={(v) => set("exit_at_t1", v)}
                label="Book full position at T1 (target-my-move mode)"
                desc="Exit the entire position at the first T1 touch. Momentum exits are disabled in this mode, so the trade runs to your target or the stop — nothing else."
              />
              <Toggle
                checked={form.use_trail}
                onChange={(v) => set("use_trail", v)}
                label="Use trailing stop"
                desc="Trail the stop to capture larger moves. Ignored when 'Book full position at T1' is on."
              />
              <Toggle checked={form.lock_at_t1} onChange={(v) => set("lock_at_t1", v)} label="Lock profit at T1" />
              <div className="flex flex-wrap gap-6">
                <NumberField label="Activate trail at R:R" value={form.trail_start_r} onChange={(v) => set("trail_start_r", v)} step={0.1} min={0} max={20} suffix="R" disabled={!form.use_trail} />
                <NumberField label="Trail ATR offset" value={form.trail_mult} onChange={(v) => set("trail_mult", v)} step={0.1} min={0.1} max={20} suffix="× ATR" disabled={!form.use_trail} />
              </div>
              <div className="flex flex-wrap gap-6">
                <NumberField label="Momentum exit confirmation" value={form.exit_confirmation_bars} onChange={(v) => set("exit_confirmation_bars", Math.round(v))} step={1} min={1} max={20} suffix="bars" />
                <NumberField label="Circuit breaker losses" value={form.max_consecutive_losses} onChange={(v) => set("max_consecutive_losses", Math.round(v))} step={1} min={1} max={20} />
                <NumberField label="Circuit breaker pause" value={form.circuit_pause_bars} onChange={(v) => set("circuit_pause_bars", Math.round(v))} step={1} min={0} max={500} suffix="bars" />
              </div>
            </div>
          </Card>

          <Card title="Session filter" desc="NSE session gate and optional manual APEX noise windows.">
            <div className="flex flex-col gap-4">
              <Toggle checked={form.use_session} onChange={(v) => set("use_session", v)} label="Enable NSE session gate" />
              <Toggle checked={form.block_open_noise} onChange={(v) => set("block_open_noise", v)} label="Block 09:15–09:30 noise zone" disabled={!form.use_session} />
              <Toggle checked={form.block_close_noise} onChange={(v) => set("block_close_noise", v)} label="Block 15:00–15:30 close zone" disabled={!form.use_session} />
            </div>
          </Card>

          <Card title="Multi-Broker & Options Engine (Upstox + Angel One)" desc="Configure 50/50 broker load balancing for equities and intraday ATM options (CE/PE) routing on Upstox.">
            <div className="flex flex-col gap-4">
              <Toggle
                checked={form.enable_options}
                onChange={(v) => set("enable_options", v)}
                label="Enable Intraday Options Engine (CE / PE)"
                desc="Dynamically resolve live Upstox NSE_FO contracts and trade options alongside or instead of cash equities."
              />
              <Toggle
                checked={form.trade_options_intraday}
                onChange={(v) => set("trade_options_intraday", v)}
                label="Auto-Execute Options on Intraday Signals"
                desc="Route 15m and 1h high-momentum signals to Upstox options orders automatically when execution mode is LIVE."
                disabled={!form.enable_options}
              />
              <div className="flex flex-col gap-2">
                <span className="text-xs text-muted-foreground">Strike Selection Mode</span>
                <Segmented
                  value={form.strike_mode as any}
                  onChange={(v) => set("strike_mode", v)}
                  options={[
                    { v: "Smart Auto", label: "Smart Auto (Delta/ATM)" },
                    { v: "Always ATM", label: "Always ATM" },
                    { v: "Always OTM1", label: "OTM +1 Strike" },
                    { v: "Always ITM1", label: "ITM +1 Strike" },
                  ]}
                />
              </div>
              <div className="flex flex-col gap-2">
                <span className="text-xs text-muted-foreground">Options Execution Broker</span>
                <Segmented
                  value={form.options_broker as any}
                  onChange={(v) => set("options_broker", v)}
                  options={[
                    { v: "upstox", label: "Upstox (Primary FNO & API v2)" },
                    { v: "angelone", label: "Angel One (Secondary)" },
                  ]}
                />
              </div>
              <div className="flex flex-col gap-2">
                <span className="text-xs text-muted-foreground">Option Stop-Loss & Risk Model</span>
                <Segmented
                  value={form.options_stop_mode as any}
                  onChange={(v) => set("options_stop_mode", v)}
                  options={[
                    { v: "Delta-Translated", label: "Delta-Translated (Underlying Risk × Δ)" },
                    { v: "Option ATR", label: "Option Contract ATR" },
                  ]}
                />
              </div>
            </div>
          </Card>
        </div>

        <p className="mt-6 text-center text-[11px] text-muted-foreground">
          These settings apply to the strategy simulation shown across the dashboard. They do not place any live broker orders.
        </p>
      </div>
    </div>
  );
}
