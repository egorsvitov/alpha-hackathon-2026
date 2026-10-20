import { useEffect, useState } from 'react';

export type Route =
  | { name: 'home' }
  | { name: 'select' }
  | { name: 'feed' }
  | { name: 'event'; eventId: string };

export function parse(hash: string): Route {
  const path = hash.replace(/^#/, '').split('?')[0] || '/';
  if (path === '/select') return { name: 'select' };
  if (path === '/news') return { name: 'feed' };
  const m = path.match(/^\/news\/(.+)$/);
  if (m) return { name: 'event', eventId: decodeURIComponent(m[1]) };
  return { name: 'home' };
}

export const href = {
  home: '#/',
  select: '#/select',
  feed: '#/news',
  event: (id: string) => `#/news/${encodeURIComponent(id)}`,
};

export function navigate(to: string) {
  window.location.hash = to;
}

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parse(window.location.hash));
  useEffect(() => {
    const on = () => {
      setRoute(parse(window.location.hash));
      window.scrollTo({ top: 0 });
    };
    window.addEventListener('hashchange', on);
    return () => window.removeEventListener('hashchange', on);
  }, []);
  return route;
}
