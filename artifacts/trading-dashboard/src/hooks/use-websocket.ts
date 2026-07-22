import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { addNotification } from "@/store/notification-store";

function fmtPrice(p: number | null | undefined): string {
  if (p == null) return "";
  return p.toFixed(2);
}

function playBeep() {
  try {
    const ctx = new (window.AudioContext || (window as any).webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    
    osc.type = "sine";
    osc.frequency.setValueAtTime(880, ctx.currentTime); // A5
    osc.frequency.exponentialRampToValueAtTime(1760, ctx.currentTime + 0.1); // A6
    
    gain.gain.setValueAtTime(0.1, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.2);
    
    osc.connect(gain);
    gain.connect(ctx.destination);
    
    osc.start();
    osc.stop(ctx.currentTime + 0.2);
  } catch (e) {
    console.error("Audio playback failed", e);
  }
}

export function useWebSocket() {
  const queryClient = useQueryClient();
  const reconnectDelay = useRef(1000);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let reconnectTimeout: ReturnType<typeof setTimeout>;
    let dead = false;

    const connect = () => {
      if (dead) return;
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const base = import.meta.env.BASE_URL.replace(/\/$/, "");
      const url = `${protocol}//${window.location.host}${base}/api/ws`;

      try { ws = new WebSocket(url); } catch { scheduleReconnect(); return; }

      ws.onopen = () => {
        reconnectDelay.current = 1000; // reset on success
      };

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data as string);
          const now = new Date().toISOString();

          // ── Invalidate queries on scan completion ─────────────────────────
          if (data.type === "scan_complete" || data.type === "connected") {
            queryClient.invalidateQueries({ queryKey: ["/api/stats"] });
            queryClient.invalidateQueries({ queryKey: ["/api/signals"] });
            queryClient.invalidateQueries({ queryKey: ["/api/trades"] });
            queryClient.invalidateQueries({ queryKey: ["/api/leaderboard"] });
            queryClient.invalidateQueries({ queryKey: ["/api/chart"] });
          }

          // ── Notification events from backend ──────────────────────────────
          if (data.type === "new_signal") {
            playBeep();
            const dir = data.direction as string;
            const emoji = dir === "BUY" ? "🟢" : "🔴";
            addNotification({
              type: "new_signal",
              title: `${emoji} ${dir} Signal — ${data.symbol}`,
              body: `${data.timeframe} | ${data.setup || "—"} | Score ${data.score ?? "—"} | ₹${fmtPrice(data.price)}  SL: ₹${fmtPrice(data.sl1)}  T1: ₹${fmtPrice(data.tp1)}`,
              symbol: data.symbol,
              timeframe: data.timeframe,
              direction: dir,
              price: data.price,
              timestamp: data.timestamp ?? now,
            });
          }

          if (data.type === "trade_triggered") {
            playBeep();
            const dir = data.direction as string;
            addNotification({
              type: "trade_triggered",
              title: `⚡ Trade Triggered — ${data.symbol}`,
              body: `${dir} @ ₹${fmtPrice(data.entry_price)} | SL ₹${fmtPrice(data.sl1)} | T1 ₹${fmtPrice(data.tp1)} | ${data.timeframe}`,
              symbol: data.symbol,
              timeframe: data.timeframe,
              direction: dir,
              price: data.entry_price,
              timestamp: data.timestamp ?? now,
            });
          }

          if (data.type === "target_hit") {
            playBeep();
            addNotification({
              type: "target_hit",
              title: `🎯 ${data.target} Hit — ${data.symbol}`,
              body: `${data.timeframe} target reached @ ₹${fmtPrice(data.price)}`,
              symbol: data.symbol,
              timeframe: data.timeframe,
              price: data.price,
              timestamp: data.timestamp ?? now,
            });
          }

          if (data.type === "tsl_update") {
            const dir = data.direction as string;
            addNotification({
              type: "tsl_update",
              title: `🛡️ Trailing Stop Moved — ${data.symbol}`,
              body: `${dir} ${data.timeframe} TSL updated to ₹${fmtPrice(data.price)}`,
              symbol: data.symbol,
              timeframe: data.timeframe,
              direction: dir,
              price: data.price,
              timestamp: data.timestamp ?? now,
            });
          }

          if (data.type === "tsl_hit") {
            const dir = data.direction as string;
            addNotification({
              type: "tsl_hit",
              title: `💰 Profit Locked — ${data.symbol}`,
              body: `${dir} ${data.timeframe} TSL hit @ ₹${fmtPrice(data.price)}`,
              symbol: data.symbol,
              timeframe: data.timeframe,
              direction: dir,
              price: data.price,
              timestamp: data.timestamp ?? now,
            });
          }

          if (data.type === "trade_transition") {
            addNotification({
              type: "trade_transition",
              title: `🔄 Trade Transition — ${data.symbol}`,
              body: `${data.timeframe} trade changed from ${data.old_type} to ${data.new_type}`,
              symbol: data.symbol,
              timeframe: data.timeframe,
              timestamp: data.timestamp ?? now,
            });
          }

          if (data.type === "sl_hit") {
            const dir = data.direction as string;
            const isRepaint = data.reason === "REPAINT_EXIT";
            addNotification({
              type: "sl_hit",
              title: isRepaint ? `⚠️ Repaint Exit - ${data.symbol}` : `🛑 Stop Loss Hit - ${data.symbol}`,
              body: isRepaint 
                ? `${dir} ${data.timeframe} signal repainted and closed @ ₹${fmtPrice(data.price)}`
                : `${dir} ${data.timeframe} stopped out @ ₹${fmtPrice(data.price)}`,
              symbol: data.symbol,
              timeframe: data.timeframe,
              direction: dir,
              price: data.price,
              timestamp: data.timestamp ?? now,
            });
          }
        } catch { /* ignore parse errors */ }
      };

      ws.onclose = () => scheduleReconnect();
      ws.onerror = () => { ws?.close(); };
    };

    const scheduleReconnect = () => {
      if (dead) return;
      // Add random jitter of ±15% to avoid thundering herd
      const jitter = reconnectDelay.current * (0.85 + Math.random() * 0.3);
      reconnectTimeout = setTimeout(() => {
        reconnectDelay.current = Math.min(reconnectDelay.current * 1.5, 30000);
        connect();
      }, jitter);
    };

    connect();
    return () => {
      dead = true;
      clearTimeout(reconnectTimeout);
      if (ws) { ws.onclose = null; ws.close(); }
    };
  }, [queryClient]);
}
