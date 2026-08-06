import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Toaster } from '@/components/ui/toaster';
import { TooltipProvider } from '@/components/ui/tooltip';
import NotFound from '@/pages/not-found';
import { Route, Switch, Router as WouterRouter } from 'wouter';
import { useEffect } from 'react';
import { Shell } from '@/components/layout/Shell';
import { ErrorBoundary } from '@/components/ErrorBoundary';

import Dashboard from '@/pages/Dashboard';
import Trades from '@/pages/Trades';
import Analytics from '@/pages/Analytics';
import ChartView from '@/pages/ChartView';
import HistoryPage from '@/pages/History';
import SettingsPage from '@/pages/SettingsPage';

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
        <Route path="/analytics" component={Analytics} />
        <Route path="/history" component={HistoryPage} />
        <Route path="/settings" component={SettingsPage} />
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
          <ErrorBoundary>
            <Router />
          </ErrorBoundary>
        </WouterRouter>
        <Toaster />
      </TooltipProvider>
    </QueryClientProvider>
  );
}

export default App;
