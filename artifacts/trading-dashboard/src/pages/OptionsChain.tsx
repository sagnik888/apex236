import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { customFetch } from "@workspace/api-client-react";
import { Activity, Layers, Search, RefreshCw, AlertTriangle } from "lucide-react";
import { Button } from "@/components/ui/button";

interface OptionRow {
  strike: number;
  ce_oi?: number;
  ce_iv?: number;
  ce_ltp?: number;
  ce_delta?: number;
  ce_theta?: number;
  pe_oi?: number;
  pe_iv?: number;
  pe_ltp?: number;
  pe_delta?: number;
  pe_theta?: number;
  is_atm?: boolean;
}

export default function OptionsChain() {
  const [symbol, setSymbol] = useState("NIFTY");
  const [expiry, setExpiry] = useState<string>("");

  const { data: symbolsData } = useQuery({
    queryKey: ["/api/symbols"],
    queryFn: () => customFetch<string[]>("/api/symbols"),
    staleTime: 60000,
  });

  const { data, isLoading, isError, refetch, isFetching } = useQuery({
    queryKey: ["/api/options/chain", symbol, expiry],
    queryFn: () => customFetch<{ expiries: string[]; chain: OptionRow[]; spot_price: number }>(`/api/options/chain?symbol=${symbol}${expiry ? `&expiry=${expiry}` : ""}`),
    refetchInterval: 10000,
    retry: false
  });

  const symbols = symbolsData || ["NIFTY", "BANKNIFTY", "FINNIFTY", "RELIANCE", "HDFCBANK"];
  const chain = data?.chain || [];
  const spotPrice = data?.spot_price;
  const expiries = data?.expiries || [];

  return (
    <div className="h-full flex flex-col bg-background overflow-hidden" id="options-chain-page">
      {/* Header */}
      <div className="flex items-center justify-between p-4 border-b border-border shrink-0">
        <div className="flex items-center gap-4">
          <Layers className="h-5 w-5 text-primary" />
          <h1 className="text-lg font-bold tracking-tight">Options Chain</h1>
          
          <div className="flex items-center gap-2 ml-4">
            <select 
              value={symbol}
              onChange={(e) => { setSymbol(e.target.value); setExpiry(""); }}
              className="bg-card border border-border rounded text-sm px-3 py-1.5 focus:outline-none focus:ring-1 focus:ring-primary font-mono"
              id="symbol-selector"
            >
              {symbols.map(s => <option key={s} value={s}>{s}</option>)}
            </select>
            
            <select 
              value={expiry}
              onChange={(e) => setExpiry(e.target.value)}
              className="bg-card border border-border rounded text-sm px-3 py-1.5 focus:outline-none focus:ring-1 focus:ring-primary font-mono"
              id="expiry-selector"
            >
              <option value="">Current Expiry</option>
              {expiries.map(e => <option key={e} value={e}>{e}</option>)}
            </select>
          </div>
        </div>

        <div className="flex items-center gap-4">
          {spotPrice != null && (
            <div className="flex items-center gap-2 bg-muted/30 px-3 py-1.5 rounded border border-border">
              <span className="text-xs text-muted-foreground uppercase font-semibold">Spot:</span>
              <span className="font-mono font-bold text-sm">₹{spotPrice.toFixed(2)}</span>
            </div>
          )}
          <Button
            variant="outline"
            size="sm"
            onClick={() => refetch()}
            disabled={isFetching}
            className="text-xs h-8"
            id="refresh-chain-btn"
          >
            <RefreshCw className={`h-3 w-3 mr-2 ${isFetching ? "animate-spin" : ""}`} />
            Refresh
          </Button>
        </div>
      </div>

      <div className="flex-1 overflow-auto relative">
        {isError ? (
           <div className="h-full flex flex-col items-center justify-center text-muted-foreground" id="options-error-state">
             <AlertTriangle className="h-8 w-8 mb-4 text-amber-500 opacity-80" />
             <p className="font-mono text-sm font-bold text-foreground">API Endpoint Unavailable</p>
             <p className="text-xs opacity-70 mt-1 max-w-md text-center">
               The /api/options/chain endpoint might not be implemented yet. This UI is ready for when the backend supports it.
             </p>
           </div>
        ) : isLoading ? (
          <div className="h-full flex flex-col items-center justify-center text-muted-foreground" id="options-loading-state">
            <Activity className="h-8 w-8 animate-pulse text-primary mb-4" />
            <p className="font-mono text-sm">Loading options chain...</p>
          </div>
        ) : chain.length === 0 ? (
          <div className="h-full flex flex-col items-center justify-center text-muted-foreground bg-card/50 rounded-lg border border-border border-dashed m-4" id="options-empty-state">
            <Search className="h-8 w-8 mb-4 opacity-50" />
            <p className="font-mono text-sm">No options data found</p>
            <p className="text-xs opacity-70 mt-1">Try selecting a different symbol or expiry.</p>
          </div>
        ) : (
          <div className="min-w-[70rem]" id="options-chain-table-container">
            <table className="w-full text-sm text-center border-collapse">
              <thead className="text-xs text-muted-foreground bg-muted/80 sticky top-0 uppercase tracking-wider z-10">
                <tr>
                  <th colSpan={5} className="py-2 border-b border-border text-signal-buy font-bold bg-signal-buy/5">CALLS</th>
                  <th className="py-2 border-b border-border w-24 bg-card/80 backdrop-blur">STRIKE</th>
                  <th colSpan={5} className="py-2 border-b border-border text-signal-sell font-bold bg-signal-sell/5">PUTS</th>
                </tr>
                <tr className="border-b border-border bg-muted/95 text-[10px]">
                  <th className="py-2 px-2 font-medium">OI</th>
                  <th className="py-2 px-2 font-medium">IV</th>
                  <th className="py-2 px-2 font-medium">Delta</th>
                  <th className="py-2 px-2 font-medium">Theta</th>
                  <th className="py-2 px-2 font-medium border-r border-border">LTP</th>
                  
                  <th className="py-2 px-2 font-medium bg-card">PRICE</th>
                  
                  <th className="py-2 px-2 font-medium border-l border-border">LTP</th>
                  <th className="py-2 px-2 font-medium">Theta</th>
                  <th className="py-2 px-2 font-medium">Delta</th>
                  <th className="py-2 px-2 font-medium">IV</th>
                  <th className="py-2 px-2 font-medium">OI</th>
                </tr>
              </thead>
              <tbody className="font-mono divide-y divide-border/50 text-xs">
                {chain.map((row, i) => {
                  const isCallItm = spotPrice != null && row.strike < spotPrice;
                  const isPutItm = spotPrice != null && row.strike > spotPrice;
                  
                  return (
                    <tr key={i} className={`hover:bg-muted/30 transition-colors ${row.is_atm ? "bg-primary/5" : ""}`} id={`strike-row-${row.strike}`}>
                      {/* Calls */}
                      <td className={`py-1.5 px-2 ${isCallItm ? "bg-blue-500/5 text-blue-100" : ""}`}>{row.ce_oi?.toLocaleString() || "—"}</td>
                      <td className={`py-1.5 px-2 ${isCallItm ? "bg-blue-500/5" : ""}`}>{row.ce_iv?.toFixed(1) || "—"}</td>
                      <td className={`py-1.5 px-2 ${isCallItm ? "bg-blue-500/5" : ""}`}>{row.ce_delta?.toFixed(2) || "—"}</td>
                      <td className={`py-1.5 px-2 text-signal-sell/70 ${isCallItm ? "bg-blue-500/5" : ""}`}>{row.ce_theta?.toFixed(2) || "—"}</td>
                      <td className={`py-1.5 px-2 font-bold text-signal-buy border-r border-border ${isCallItm ? "bg-blue-500/5" : ""}`}>{row.ce_ltp?.toFixed(2) || "—"}</td>
                      
                      {/* Strike */}
                      <td className={`py-1.5 px-2 font-bold bg-card ${row.is_atm ? "text-primary bg-primary/10 border-x-2 border-primary" : ""}`}>
                        {row.strike}
                      </td>
                      
                      {/* Puts */}
                      <td className={`py-1.5 px-2 font-bold text-signal-sell border-l border-border ${isPutItm ? "bg-blue-500/5" : ""}`}>{row.pe_ltp?.toFixed(2) || "—"}</td>
                      <td className={`py-1.5 px-2 text-signal-sell/70 ${isPutItm ? "bg-blue-500/5" : ""}`}>{row.pe_theta?.toFixed(2) || "—"}</td>
                      <td className={`py-1.5 px-2 ${isPutItm ? "bg-blue-500/5" : ""}`}>{row.pe_delta?.toFixed(2) || "—"}</td>
                      <td className={`py-1.5 px-2 ${isPutItm ? "bg-blue-500/5" : ""}`}>{row.pe_iv?.toFixed(1) || "—"}</td>
                      <td className={`py-1.5 px-2 ${isPutItm ? "bg-blue-500/5 text-blue-100" : ""}`}>{row.pe_oi?.toLocaleString() || "—"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
