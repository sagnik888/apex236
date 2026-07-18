import { useEffect, useRef, useState } from "react";
import { Bell, BellOff, X, Trash2, ArrowUpRight, ArrowDownRight, Target, ShieldAlert, Zap } from "lucide-react";
import {
  type Notification,
  subscribe,
  markAllRead,
  clearNotifications,
  requestBrowserPermission,
} from "@/store/notification-store";

function NotifIcon({ type, direction }: { type: string; direction?: string }) {
  if (type === "new_signal") return direction === "BUY" ? <ArrowUpRight className="h-3.5 w-3.5 text-signal-buy" /> : <ArrowDownRight className="h-3.5 w-3.5 text-signal-sell" />;
  if (type === "trade_triggered") return <Zap className="h-3.5 w-3.5 text-yellow-400" />;
  if (type === "target_hit") return <Target className="h-3.5 w-3.5 text-signal-buy" />;
  if (type === "sl_hit") return <ShieldAlert className="h-3.5 w-3.5 text-signal-sell" />;
  return <Bell className="h-3.5 w-3.5 text-muted-foreground" />;
}

function TypeBg({ type }: { type: string }) {
  const cls =
    type === "new_signal"     ? "bg-primary/10 border-primary/20" :
    type === "trade_triggered"? "bg-yellow-500/10 border-yellow-500/20" :
    type === "target_hit"     ? "bg-signal-buy/10 border-signal-buy/20" :
    type === "sl_hit"         ? "bg-signal-sell/10 border-signal-sell/20" :
    "bg-muted border-border";
  return cls;
}

function fmtTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: false });
  } catch { return ""; }
}

// ── Toast strip (bottom-right) ────────────────────────────────────────────────

export function NotificationToasts() {
  const [toasts, setToasts] = useState<Notification[]>([]);
  const seenIds = useRef(new Set<string>());
  const prevList = useRef<Notification[]>([]);

  useEffect(() => {
    return subscribe((list) => {
      const newOnes = list.filter(n => !seenIds.current.has(n.id));
      newOnes.forEach(n => seenIds.current.add(n.id));
      if (newOnes.length > 0) {
        setToasts(prev => [...newOnes, ...prev].slice(0, 5));
        newOnes.forEach(n => {
          setTimeout(() => {
            setToasts(prev => prev.filter(t => t.id !== n.id));
          }, 7000);
        });
      }
      prevList.current = list;
    });
  }, []);

  if (toasts.length === 0) return null;

  return (
    <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 max-w-sm">
      {toasts.map(t => (
        <div
          key={t.id}
          className={`flex items-start gap-2.5 p-3 rounded-lg border shadow-lg backdrop-blur-sm animate-in slide-in-from-right-4 duration-300 ${TypeBg({ type: t.type })} bg-card/95`}
        >
          <div className="pt-0.5 flex-shrink-0">
            <NotifIcon type={t.type} direction={t.direction} />
          </div>
          <div className="flex-1 min-w-0">
            <div className="font-semibold text-xs text-foreground leading-snug truncate">{t.title}</div>
            <div className="text-[11px] text-muted-foreground mt-0.5 leading-snug">{t.body}</div>
          </div>
          <button
            onClick={() => setToasts(p => p.filter(x => x.id !== t.id))}
            className="flex-shrink-0 p-0.5 hover:text-foreground text-muted-foreground transition-colors"
          >
            <X className="h-3 w-3" />
          </button>
        </div>
      ))}
    </div>
  );
}

// ── Bell + Dropdown ───────────────────────────────────────────────────────────

export function NotificationBell() {
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [open, setOpen] = useState(false);
  const [permission, setPermission] = useState<NotificationPermission>(
    typeof Notification !== "undefined" ? Notification.permission : "denied"
  );
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    return subscribe(setNotifications);
  }, []);

  // Close on outside click
  useEffect(() => {
    function handler(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const unread = notifications.filter(n => !n.read).length;

  const handleBell = () => {
    if (!open) markAllRead();
    setOpen(p => !p);
  };

  const enableNotifications = async () => {
    const p = await requestBrowserPermission();
    setPermission(p);
  };

  return (
    <div ref={ref} className="relative">
      <button
        onClick={handleBell}
        className={`relative p-1.5 rounded-md transition-colors ${open ? "bg-primary/10 text-primary" : "text-muted-foreground hover:text-foreground hover:bg-muted"}`}
        title="Notifications"
      >
        <Bell className="h-4 w-4" />
        {unread > 0 && (
          <span className="absolute -top-1 -right-1 h-4 w-4 flex items-center justify-center bg-signal-sell text-white text-[9px] font-bold rounded-full">
            {unread > 9 ? "9+" : unread}
          </span>
        )}
      </button>

      {open && (
        <div className="absolute right-0 top-full mt-2 w-80 bg-card border border-border rounded-lg shadow-2xl z-50 overflow-hidden">
          {/* Header */}
          <div className="flex items-center justify-between px-3 py-2 border-b border-border bg-muted/30">
            <span className="text-xs font-bold uppercase tracking-wider text-muted-foreground">
              Alerts {notifications.length > 0 && `(${notifications.length})`}
            </span>
            <div className="flex items-center gap-1">
              {permission !== "granted" && (
                <button
                  onClick={enableNotifications}
                  title="Enable browser notifications"
                  className="p-1 rounded text-muted-foreground hover:text-primary transition-colors"
                >
                  <BellOff className="h-3.5 w-3.5" />
                </button>
              )}
              {notifications.length > 0 && (
                <button
                  onClick={clearNotifications}
                  title="Clear all"
                  className="p-1 rounded text-muted-foreground hover:text-signal-sell transition-colors"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              )}
            </div>
          </div>

          {/* Browser permission banner */}
          {permission === "default" && (
            <button
              onClick={enableNotifications}
              className="w-full px-3 py-2 text-xs text-center text-yellow-400 bg-yellow-500/10 border-b border-yellow-500/20 hover:bg-yellow-500/20 transition-colors"
            >
              Click to enable browser push notifications
            </button>
          )}

          {/* List */}
          <div className="max-h-80 overflow-y-auto">
            {notifications.length === 0 ? (
              <div className="flex flex-col items-center py-8 text-muted-foreground">
                <Bell className="h-6 w-6 mb-2 opacity-40" />
                <span className="text-xs">No alerts yet</span>
                <span className="text-[11px] opacity-60 mt-0.5">Signals fire here in real time</span>
              </div>
            ) : (
              notifications.map(n => (
                <div key={n.id} className={`flex gap-2.5 px-3 py-2.5 border-b border-border/50 last:border-0 hover:bg-muted/30 ${TypeBg({ type: n.type })}`}>
                  <div className="pt-0.5 flex-shrink-0">
                    <NotifIcon type={n.type} direction={n.direction} />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-start justify-between gap-2">
                      <span className="text-xs font-semibold text-foreground leading-tight">{n.title}</span>
                      <span className="text-[10px] text-muted-foreground flex-shrink-0 font-mono">{fmtTime(n.timestamp)}</span>
                    </div>
                    <div className="text-[11px] text-muted-foreground mt-0.5 leading-snug">{n.body}</div>
                  </div>
                </div>
              ))
            )}
          </div>
        </div>
      )}
    </div>
  );
}
