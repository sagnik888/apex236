import React from 'react';

export default function ScreenerView() {
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
