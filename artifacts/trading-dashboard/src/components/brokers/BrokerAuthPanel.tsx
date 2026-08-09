import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, ExternalLink, KeyRound, Loader2, RefreshCw, X } from "lucide-react";

/**
 * Broker connection badges with an inline Upstox re-authentication flow.
 *
 * Upstox tokens die at 03:30 IST every day and cannot be renewed
 * programmatically — the exchange step needs a human browser login. Before
 * this, the operator discovered a lapsed session when an order failed. The
 * badge now carries the countdown and doubles as the reconnect button.
 *
 * AngelOne holds a TOTP secret and re-mints its own session, so it is
 * status-only: there is nothing for a human to do.
 */

type UpstoxAuth = {
  connected: boolean;
  auth_required: boolean;
  reason: string;
  user_id: string;
  expires_at: string | null;
  hours_remaining: number | null;
  seconds_remaining: number | null;
  expiring_soon: boolean;
  daily_cutoff_ist: string;
  missing_credentials: string[];
};

type BrokerStatus = {
  dispatcher?: {
    equity_provider: string | null;
    options_provider: string | null;
    options_available: boolean;
    options_status: string;
    split_ratio: string;
    angel_available: boolean;
    upstox_available: boolean;
    angel_assigned_count: number;
    upstox_assigned_count: number;
    upstox_auth?: UpstoxAuth;
  };
};

async function getJson<T>(url: string): Promise<T> {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

function Badge({
  label, detail, tone, onClick, title,
}: {
  label: string; detail: string;
  tone: "ok" | "warn" | "bad";
  onClick?: () => void; title?: string;
}) {
  const tones = {
    ok: "border-signal-buy/30 bg-signal-buy/[0.08] text-signal-buy",
    warn: "border-amber-500/40 bg-amber-500/[0.10] text-amber-300",
    bad: "border-signal-sell/40 bg-signal-sell/[0.10] text-signal-sell",
  } as const;
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      disabled={!onClick}
      className={`flex flex-col items-start gap-0.5 rounded-md border px-3 py-2 text-left transition-colors ${tones[tone]} ${
        onClick ? "cursor-pointer hover:brightness-125" : "cursor-default"
      }`}
    >
      <span className="font-mono text-[11px] font-bold tracking-wider">{label}</span>
      <span className="font-mono text-[10px] opacity-80">{detail}</span>
    </button>
  );
}

export default function BrokerAuthPanel() {
  const qc = useQueryClient();
  const [modalOpen, setModalOpen] = useState(false);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ kind: "ok" | "err" | "info"; text: string } | null>(null);
  const [loginUrl, setLoginUrl] = useState<string | null>(null);

  const { data } = useQuery({
    queryKey: ["/api/brokers/status"],
    queryFn: () => getJson<BrokerStatus>("/api/brokers/status"),
    refetchInterval: 10000,
  });

  const d = data?.dispatcher;
  const auth = d?.upstox_auth;

  // Nag once when the session lapses, rather than every poll.
  const [nagged, setNagged] = useState(false);
  useEffect(() => {
    if (auth?.auth_required && !nagged) {
      setNagged(true);
      setMessage({ kind: "err", text: "Upstox login required — options are disabled until you reconnect." });
    }
    if (!auth?.auth_required && nagged) setNagged(false);
  }, [auth?.auth_required, nagged]);

  /**
   * Start the OAuth flow.
   *
   * The popup is opened SYNCHRONOUSLY inside the click handler and only then
   * redirected, because a window.open() issued after an await is treated as
   * unsolicited and blocked by every browser.
   */
  async function startUpstoxLogin() {
    setMessage(null);
    let win: Window | null = null;
    try {
      win = window.open("about:blank", "_blank");
      if (win) {
        win.opener = null;
        win.document.write(
          '<!doctype html><title>Upstox Login</title><body style="font-family:sans-serif;background:#090E17;color:#94A3B8;padding:24px">Preparing Upstox login…</body>'
        );
      }
      const res = await getJson<UpstoxAuth & { login_url: string | null }>("/api/auth/upstox");
      if (!res.login_url) {
        win?.close();
        setMessage({
          kind: "err",
          text: res.missing_credentials?.length
            ? `Missing in upstox_secrets.env: ${res.missing_credentials.join(", ")}`
            : res.reason || "Could not build the Upstox login URL",
        });
        return;
      }
      setLoginUrl(res.login_url);
      if (win && !win.closed) win.location.href = res.login_url;
      setModalOpen(true);
      setMessage({
        kind: "info",
        text: "Log in, then paste the FULL redirect URL below. The code expires in ~2 minutes.",
      });
    } catch (err) {
      win?.close();
      setMessage({ kind: "err", text: `Could not start the Upstox login: ${(err as Error).message}` });
    }
  }

  async function submitCode() {
    if (!code.trim()) return;
    setBusy(true);
    setMessage(null);
    try {
      const r = await fetch("/api/auth/upstox", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: code.trim() }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(body.detail || `${r.status} ${r.statusText}`);
      setMessage({ kind: "ok", text: "Upstox connected. Options are live." });
      setCode("");
      setModalOpen(false);
      qc.invalidateQueries({ queryKey: ["/api/brokers/status"] });
    } catch (err) {
      setMessage({ kind: "err", text: (err as Error).message });
    } finally {
      setBusy(false);
    }
  }

  async function recheck() {
    setBusy(true);
    try {
      await fetch("/api/auth/upstox/recheck", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      qc.invalidateQueries({ queryKey: ["/api/brokers/status"] });
      setMessage({ kind: "info", text: "Re-probed the cached Upstox token." });
    } finally {
      setBusy(false);
    }
  }

  const upstoxTone: "ok" | "warn" | "bad" = !auth || auth.auth_required
    ? "bad"
    : auth.expiring_soon
      ? "warn"
      : "ok";

  const upstoxDetail = !auth
    ? "status unavailable"
    : auth.auth_required
      ? "LOGIN REQUIRED — click"
      : auth.hours_remaining != null
        ? `${auth.hours_remaining}h left · resets ${auth.daily_cutoff_ist}`
        : "connected";

  return (
    <div className="rounded-lg border border-border bg-card/40 p-3">
      <div className="mb-2 flex items-center justify-between">
        <h3 className="flex items-center gap-2 text-xs font-bold uppercase tracking-wider text-muted-foreground">
          <KeyRound className="h-3.5 w-3.5" /> Broker Connections
        </h3>
        <button
          onClick={recheck}
          disabled={busy}
          title="Re-probe the cached Upstox token"
          className="flex items-center gap-1 rounded border border-border px-2 py-1 text-[11px] text-muted-foreground hover:text-foreground disabled:opacity-50"
        >
          {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
          Re-check
        </button>
      </div>

      <div className="flex flex-wrap gap-2">
        <Badge
          label="ANGELONE · EQUITY"
          detail={
            d?.angel_available
              ? `${d.angel_assigned_count} symbols · auto-renews`
              : "offline"
          }
          tone={d?.angel_available ? "ok" : "bad"}
          title="AngelOne holds a TOTP secret and re-mints its own session daily — nothing to do here."
        />
        <Badge
          label="UPSTOX · OPTIONS"
          detail={upstoxDetail}
          tone={upstoxTone}
          onClick={startUpstoxLogin}
          title={
            auth?.auth_required
              ? `Upstox login required: ${auth.reason}. Click to reconnect.`
              : `Upstox session. Expires ${auth?.expires_at ?? "—"}. Click to re-authenticate early.`
          }
        />
      </div>

      {d && (
        <p className="mt-2 font-mono text-[10px] text-muted-foreground">{d.split_ratio}</p>
      )}

      {message && (
        <div
          className={`mt-2 flex items-start gap-2 rounded border px-2 py-1.5 text-[11px] ${
            message.kind === "ok"
              ? "border-signal-buy/40 bg-signal-buy/10 text-signal-buy"
              : message.kind === "err"
                ? "border-signal-sell/40 bg-signal-sell/10 text-signal-sell"
                : "border-border bg-muted/20 text-muted-foreground"
          }`}
        >
          {message.kind === "ok" ? (
            <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          ) : message.kind === "err" ? (
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          ) : null}
          <span className="break-words">{message.text}</span>
        </div>
      )}

      {modalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
          <div className="w-full max-w-lg rounded-lg border border-border bg-card p-4 shadow-xl">
            <div className="mb-3 flex items-center justify-between">
              <h4 className="text-sm font-bold text-foreground">Complete the Upstox login</h4>
              <button onClick={() => setModalOpen(false)} className="text-muted-foreground hover:text-foreground">
                <X className="h-4 w-4" />
              </button>
            </div>

            <ol className="mb-3 space-y-1.5 text-[11px] text-muted-foreground">
              <li>1. Log in and complete 2FA in the tab that opened.</li>
              <li>
                2. You land on a page that fails to load — that is expected. Copy the{" "}
                <span className="text-foreground">whole address bar</span>.
              </li>
              <li>3. Paste it below. A truncated code is the most common failure.</li>
            </ol>

            {loginUrl && (
              <a
                href={loginUrl}
                target="_blank"
                rel="noreferrer noopener"
                className="mb-3 inline-flex items-center gap-1 text-[11px] text-primary hover:underline"
              >
                <ExternalLink className="h-3 w-3" /> Re-open the login page
              </a>
            )}

            <input
              autoFocus
              value={code}
              onChange={(e) => setCode(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && submitCode()}
              placeholder="http://127.0.0.1:5173/?code=…  (or just the code)"
              className="w-full rounded border border-border bg-background px-2 py-1.5 font-mono text-xs text-foreground outline-none focus:border-primary"
            />

            <div className="mt-3 flex justify-end gap-2">
              <button
                onClick={() => setModalOpen(false)}
                className="rounded border border-border px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground"
              >
                Cancel
              </button>
              <button
                onClick={submitCode}
                disabled={busy || !code.trim()}
                className="flex items-center gap-1.5 rounded bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground disabled:opacity-50"
              >
                {busy && <Loader2 className="h-3 w-3 animate-spin" />}
                Connect
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
