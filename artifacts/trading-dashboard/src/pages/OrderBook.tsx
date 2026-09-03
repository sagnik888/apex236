import { useQuery } from "@tanstack/react-query";
import { customFetch } from "@workspace/api-client-react";
import { List, Activity, AlertTriangle, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";

interface OrderEvent {
  id: string;
  time: string;
  symbol: string;
  direction: string;
  order_type: string;
  broker: string;
  status: string;
  fill_price?: number;
  signal_price?: number;
  slippage?: number;
}

export default function OrderBook() {
  const { data, isLoading, isError, refetch, isFetching } = useQuery({
    queryKey: ["/api/orders/book"],
    queryFn: () => customFetch<{ intents: OrderEvent[], fills: OrderEvent[] }>("/api/orders/book"),
    refetchInterval: 5000,
    retry: false
  });

  const orders = [...(data?.intents || []), ...(data?.fills || [])].sort((a, b) => new Date(b.time).getTime() - new Date(a.time).getTime());

  const getStatusBadge = (status: string) => {
    switch (status.toUpperCase()) {
      case "FILLED":
      case "COMPLETE":
        return "bg-emerald-500/20 text-emerald-400 border-emerald-500/30";
      case "REJECTED":
      case "FAILED":
        return "bg-rose-500/20 text-rose-400 border-rose-500/30";
      case "PENDING":
      case "OPEN":
        return "bg-amber-500/20 text-amber-400 border-amber-500/30";
      case "CANCELLED":
        return "bg-gray-500/20 text-gray-400 border-gray-500/30";
      default:
        return "bg-muted text-muted-foreground border-border";
    }
  };

  const fmtTime = (iso: string) => {
    try {
      const d = new Date(iso);
      return d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone: "Asia/Kolkata" });
    } catch {
      return iso;
    }
  };

  return (
    <div className="h-full flex flex-col bg-background overflow-hidden" id="order-book-page">
      <div className="flex items-center justify-between p-4 border-b border-border shrink-0">
        <div className="flex items-center gap-4">
          <List className="h-5 w-5 text-primary" />
          <h1 className="text-lg font-bold tracking-tight">Order Book</h1>
          <span className="text-xs text-muted-foreground bg-muted px-2 py-1 rounded">
            Live Feed
          </span>
        </div>
        
        <Button
          variant="outline"
          size="sm"
          onClick={() => refetch()}
          disabled={isFetching}
          className="text-xs h-8"
          id="refresh-orders-btn"
        >
          <RefreshCw className={`h-3 w-3 mr-2 ${isFetching ? "animate-spin" : ""}`} />
          Refresh
        </Button>
      </div>

      <div className="flex-1 overflow-auto p-4">
        {isError ? (
          <div className="h-full flex flex-col items-center justify-center text-muted-foreground" id="orders-error-state">
            <AlertTriangle className="h-8 w-8 mb-4 text-amber-500 opacity-80" />
            <p className="font-mono text-sm font-bold text-foreground">API Endpoint Unavailable</p>
            <p className="text-xs opacity-70 mt-1">The /api/orders/book endpoint is not ready yet.</p>
          </div>
        ) : isLoading ? (
          <div className="h-full flex items-center justify-center text-muted-foreground" id="orders-loading-state">
            <Activity className="h-8 w-8 animate-pulse text-primary mb-4" />
            <p className="font-mono text-sm mt-4">Loading orders...</p>
          </div>
        ) : orders.length === 0 ? (
          <div className="h-full flex flex-col items-center justify-center text-muted-foreground bg-card/50 rounded-lg border border-border border-dashed" id="orders-empty-state">
            <List className="h-10 w-10 mb-4 opacity-40" />
            <p className="font-mono text-sm">No orders today</p>
          </div>
        ) : (
          <div className="rounded-md border border-border bg-card overflow-hidden" id="orders-table">
            <table className="w-full text-sm text-left">
              <thead className="text-xs text-muted-foreground bg-muted/50 border-b border-border uppercase tracking-wider">
                <tr>
                  <th className="px-4 py-3 font-medium">Time (IST)</th>
                  <th className="px-4 py-3 font-medium">Symbol</th>
                  <th className="px-4 py-3 font-medium">Dir</th>
                  <th className="px-4 py-3 font-medium">Type</th>
                  <th className="px-4 py-3 font-medium">Broker</th>
                  <th className="px-4 py-3 font-medium">Status</th>
                  <th className="px-4 py-3 font-medium text-right">Fill Px</th>
                  <th className="px-4 py-3 font-medium text-right">Signal Px</th>
                  <th className="px-4 py-3 font-medium text-right">Slippage</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {orders.map((order, i) => (
                  <tr key={order.id || i} className="hover:bg-muted/30 transition-colors font-mono text-xs" id={`order-row-${order.id || i}`}>
                    <td className="px-4 py-2.5 text-muted-foreground">{fmtTime(order.time)}</td>
                    <td className="px-4 py-2.5 font-bold text-foreground">{order.symbol}</td>
                    <td className="px-4 py-2.5">
                      <span className={order.direction === "BUY" ? "text-signal-buy" : "text-signal-sell"}>
                        {order.direction}
                      </span>
                    </td>
                    <td className="px-4 py-2.5 text-muted-foreground">{order.order_type}</td>
                    <td className="px-4 py-2.5">{order.broker}</td>
                    <td className="px-4 py-2.5">
                      <span className={`px-2 py-0.5 rounded border text-[10px] font-bold tracking-wider ${getStatusBadge(order.status)}`}>
                        {order.status}
                      </span>
                    </td>
                    <td className="px-4 py-2.5 text-right font-bold">
                      {order.fill_price ? `₹${order.fill_price.toFixed(2)}` : "—"}
                    </td>
                    <td className="px-4 py-2.5 text-right text-muted-foreground">
                      {order.signal_price ? `₹${order.signal_price.toFixed(2)}` : "—"}
                    </td>
                    <td className="px-4 py-2.5 text-right">
                      {order.slippage != null ? (
                        <span className={order.slippage > 0 ? "text-rose-400" : order.slippage < 0 ? "text-emerald-400" : ""}>
                          {order.slippage > 0 ? "+" : ""}{order.slippage.toFixed(2)}%
                        </span>
                      ) : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
