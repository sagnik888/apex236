/**
 * Lightweight module-level notification store.
 * No Redux — just a simple observable list of recent alerts.
 */

export type NotifType =
  | "new_signal"
  | "trade_triggered"
  | "target_hit"
  | "sl_hit"
  | "scan_complete";

export interface Notification {
  id: string;
  type: NotifType;
  title: string;
  body: string;
  symbol?: string;
  timeframe?: string;
  direction?: string;
  price?: number;
  timestamp: string;
  read: boolean;
}

type Listener = (notifications: Notification[]) => void;

let _notifications: Notification[] = [];
const _listeners = new Set<Listener>();

function notify(listeners: Set<Listener>, data: Notification[]) {
  listeners.forEach(l => {
    try { l(data); } catch { /* ignore */ }
  });
}

export function addNotification(evt: Omit<Notification, "id" | "read">): void {
  const n: Notification = { ...evt, id: crypto.randomUUID(), read: false };
  _notifications = [n, ..._notifications].slice(0, 50); // keep last 50
  notify(_listeners, [..._notifications]);

  // Browser Notification API
  if (typeof Notification !== "undefined" && Notification.permission === "granted") {
    try {
      const bn = new window.Notification(n.title, {
        body: n.body,
        icon: "/favicon.ico",
        tag: `${n.symbol ?? "apex"}-${n.type}`,
        silent: false,
      });
      bn.onclick = () => window.focus();
    } catch { /* ignore if blocked */ }
  }
}

export function markAllRead(): void {
  _notifications = _notifications.map(n => ({ ...n, read: true }));
  notify(_listeners, [..._notifications]);
}

export function clearNotifications(): void {
  _notifications = [];
  notify(_listeners, []);
}

export function subscribe(listener: Listener): () => void {
  _listeners.add(listener);
  listener([..._notifications]); // immediate snapshot
  return () => { _listeners.delete(listener); };
}

export function getSnapshot(): Notification[] {
  return [..._notifications];
}

export function requestBrowserPermission(): Promise<NotificationPermission> {
  if (typeof Notification === "undefined") return Promise.resolve("denied");
  if (Notification.permission === "granted") return Promise.resolve("granted");
  return Notification.requestPermission();
}
