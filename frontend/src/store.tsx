import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import type { CompanyProfile, DemoSettings, DemoUser, Role } from './types';
import { api, demoSettings } from './api';
import { COMPANIES } from './data/companies';

type Theme = 'light' | 'dark';
/** Оформление: «cabinet» — кабинет по образцу Альфа-Бизнеса, «classic» — первая версия (бенто). */
export type Design = 'cabinet' | 'classic';
const KEY = 'alfa-news-demo-state-v1';

interface Persisted {
  companyId: string | null;
  role: Role | null;
  theme: Theme;
  user: DemoUser | null;
  /** скрытые карточки по компании: companyId → event_id[] */
  hidden: Record<string, string[]>;
  /** включены ли браузерные уведомления о важных событиях */
  push: boolean;
  design: Design;
}

/** Демо-вход. В банке логин привязан к физлицу, у которого есть доступ к нескольким компаниям. */
export const DEMO_LOGIN = { login: 'demo', password: 'alfa2026', name: 'Иван Иванович Иванов' };

const read = (): Persisted => {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) return { user: null, hidden: {}, push: false, design: 'cabinet', ...(JSON.parse(raw) as Partial<Persisted>) } as Persisted;
  } catch { /* localStorage недоступен */ }
  const dark = typeof window !== 'undefined' && window.matchMedia?.('(prefers-color-scheme: dark)').matches;
  return { companyId: null, role: null, theme: dark ? 'dark' : 'light', user: null, hidden: {}, push: false, design: 'cabinet' };
};

interface AppState {
  companies: CompanyProfile[];
  companiesLoading: boolean;
  companiesError: string | null;
  company: CompanyProfile | null;
  role: Role | null;
  theme: Theme;
  user: DemoUser | null;
  /** event_id скрытых карточек текущей компании */
  hidden: string[];
  push: boolean;
  design: Design;
  demo: DemoSettings;
  /** увеличивается при загрузке новой новости — лента и виджет перезапрашиваются */
  feedVersion: number;
  login(login: string, password: string): boolean;
  logout(): void;
  hide(eventId: string): void;
  unhide(eventId: string): void;
  setPush(on: boolean): void;
  setDesign(design: Design): void;
  select(companyId: string, role: Role): void;
  setRole(role: Role): void;
  setCompany(companyId: string): void;
  toggleTheme(): void;
  updateDemo(patch: Partial<DemoSettings>): void;
  bumpFeed(): void;
}

const Ctx = createContext<AppState | null>(null);

export function AppProvider({ children }: { children: ReactNode }) {
  const [p, setP] = useState<Persisted>(read);
  const [companies, setCompanies] = useState<CompanyProfile[]>(api.mode === 'http' ? [] : COMPANIES);
  const [companiesLoading, setCompaniesLoading] = useState(api.mode === 'http');
  const [companiesError, setCompaniesError] = useState<string | null>(null);
  const [demo, setDemo] = useState<DemoSettings>({ ...demoSettings });
  const [feedVersion, setFeedVersion] = useState(0);

  useEffect(() => {
    let alive = true;
    api.getCompanies().then(
      data => { if (alive) { setCompanies(data); setCompaniesError(null); } },
      error => { if (alive) setCompaniesError(error instanceof Error ? error.message : 'Не удалось загрузить компании'); },
    ).finally(() => { if (alive) setCompaniesLoading(false); });
    return () => { alive = false; };
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = p.theme;
    document.documentElement.dataset.design = p.design;
    try { localStorage.setItem(KEY, JSON.stringify(p)); } catch { /* ignore */ }
  }, [p]);

  const value = useMemo<AppState>(
    () => ({
      companies,
      companiesLoading,
      companiesError,
      company: companies.find((c) => c.company_id === p.companyId) ?? null,
      role: p.role,
      theme: p.theme,
      user: p.user,
      hidden: (p.companyId && p.hidden[p.companyId]) || [],
      push: p.push,
      design: p.design,
      demo,
      feedVersion,
      login: (login, password) => {
        const ok = login.trim().toLowerCase() === DEMO_LOGIN.login && password === DEMO_LOGIN.password;
        if (ok) setP((s) => ({ ...s, user: { login: DEMO_LOGIN.login, name: DEMO_LOGIN.name } }));
        return ok;
      },
      logout: () => setP((s) => ({ ...s, user: null, companyId: null, role: null })),
      hide: (eventId) => setP((s) => {
        if (!s.companyId) return s;
        const cur = s.hidden[s.companyId] ?? [];
        return cur.includes(eventId) ? s : { ...s, hidden: { ...s.hidden, [s.companyId]: [...cur, eventId] } };
      }),
      unhide: (eventId) => setP((s) => (s.companyId
        ? { ...s, hidden: { ...s.hidden, [s.companyId]: (s.hidden[s.companyId] ?? []).filter((x) => x !== eventId) } }
        : s)),
      setPush: (on) => setP((s) => ({ ...s, push: on })),
      setDesign: (design) => setP((s) => ({ ...s, design })),
      select: (companyId, role) => setP((s) => ({ ...s, companyId, role })),
      setRole: (role) => setP((s) => ({ ...s, role })),
      setCompany: (companyId) => setP((s) => ({ ...s, companyId })),
      toggleTheme: () => setP((s) => ({ ...s, theme: s.theme === 'light' ? 'dark' : 'light' })),
      updateDemo: (patch) => {
        Object.assign(demoSettings, patch);
        setDemo({ ...demoSettings });
      },
      bumpFeed: () => setFeedVersion((v) => v + 1),
    }),
    [companies, companiesLoading, companiesError, p, demo, feedVersion],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useApp(): AppState {
  const v = useContext(Ctx);
  if (!v) throw new Error('AppProvider отсутствует');
  return v;
}

/* ---------- простой async-хук ---------- */

export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]) {
  const [state, setState] = useState<{ data: T | null; error: string | null; loading: boolean }>({ data: null, error: null, loading: true });
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let alive = true;
    setState((s) => ({ ...s, loading: true, error: null }));
    fn().then(
      (data) => alive && setState({ data, error: null, loading: false }),
      (e: unknown) => alive && setState({ data: null, error: e instanceof Error ? e.message : 'Неизвестная ошибка', loading: false }),
    );
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { ...state, reload };
}
