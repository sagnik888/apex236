import React, { useEffect } from 'react';
import { useLocation } from "wouter";

export default function ScreenerView() {
  const [, setLocation] = useLocation();

  useEffect(() => {
    const handleMessage = (event: MessageEvent) => {
      if (event.data && event.data.type === 'OPEN_CHART') {
        let tf = event.data.timeframe;
        // Map screener micro timeframes to the standard 15m default if they don't map directly to backend chart timeframes
        if (tf === '30min') {
           tf = '15m'; 
        } else {
           tf = tf.replace('min', 'm');
        }
        setLocation(`/chart/${encodeURIComponent(event.data.symbol)}/${tf}`);
      }
    };
    window.addEventListener('message', handleMessage);
    return () => window.removeEventListener('message', handleMessage);
  }, [setLocation]);

  return (
    <div className="w-full h-full">
      <iframe 
        src="http://127.0.0.1:8000/" 
        className="w-full h-full border-none" 
        title="System Screener" 
      />
    </div>
  );
}
