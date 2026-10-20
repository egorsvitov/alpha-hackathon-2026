import { useEffect } from 'react';
import { AppProvider, useApp } from './store';
import { href, navigate, useRoute } from './router';
import { BottomNav, FeedProvider, Header, Sidebar } from './components/Shell';
import { SelectPage } from './pages/SelectPage';
import { HomePage } from './pages/HomePage';
import { FeedPage } from './pages/FeedPage';
import { EventPage } from './pages/EventPage';

function Routes() {
  const route = useRoute();
  const { company, role } = useApp();
  const authed = Boolean(company && role);

  useEffect(() => {
    if (!authed && route.name !== 'select') navigate(href.select);
  }, [authed, route.name]);

  if (!authed || route.name === 'select') {
    return (
      <>
        <a className="skip-link" href="#main">К содержимому</a>
        <main id="main"><SelectPage /></main>
      </>
    );
  }

  return (
    <FeedProvider>
      <a className="skip-link" href="#main">К содержимому</a>
      <Header />
      <div className="app-body">
        <Sidebar route={route} />
        <main id="main" className="app-main">
          {route.name === 'home' && <HomePage />}
          {route.name === 'feed' && <FeedPage />}
          {route.name === 'event' && <EventPage eventId={route.eventId} />}
        </main>
      </div>
      <BottomNav route={route} />
    </FeedProvider>
  );
}

export default function App() {
  return (
    <AppProvider>
      <Routes />
    </AppProvider>
  );
}
