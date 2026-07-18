import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Toaster } from '@/components/ui/toaster';
import { TooltipProvider } from '@/components/ui/tooltip';
import NotFound from '@/pages/not-found';
import { Route, Switch, Router as WouterRouter } from 'wouter';
import { useEffect } from 'react';
import { Shell } from '@/components/layout/Shell';

import Dashboard from '@/pages/Dashboard';
import Trades from '@/pages/Trades';
import ChartView from '@/pages/ChartView';

const queryClient = new QueryClient();

function AppEffects() {
  // useWebSocket is now called inside Shell to avoid duplicate hook calls
  useEffect(() => {
    document.documentElement.classList.add('dark');
  }, []);
  
  return null;
}

function Router() {
  return (
    <Shell>
      <Switch>
        <Route path="/" component={Dashboard} />
        <Route path="/trades" component={Trades} />
        <Route path="/chart/:symbol/:timeframe" component={ChartView} />
        <Route component={NotFound} />
      </Switch>
    </Shell>
  );
}

function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <WouterRouter base={import.meta.env.BASE_URL.replace(/\/$/, '')}>
          <AppEffects />
          <Router />
        </WouterRouter>
        <Toaster />
      </TooltipProvider>
    </QueryClientProvider>
  );
}

export default App;
