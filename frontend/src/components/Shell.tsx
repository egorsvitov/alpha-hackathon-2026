import { createContext, useContext, type ReactNode } from 'react';
import {
  Bell, Check, ChevronDown, FileText, FlaskConical, Home, MoreHorizontal, Moon, Newspaper, Search, Sun, User, Wallet, ArrowLeftRight,
} from 'lucide-react';
import { api, resetDemoNews } from '../api';
import type { FeedItem } from '../types';
import { ROLES, initialsOf, roleLabel } from '../data/companies';
import { href, navigate, type Route } from '../router';
import { useApp, useAsync } from '../store';
import { Badge, Button, IconButton, usePopover } from './ui';

/* ---------------- Лента как общий ресурс (виджет, счётчик, страница ленты) ---------------- */

interface FeedCtx { items: FeedItem[] | null; loading: boolean; error: string | null; reload(): void }
const FeedContext = createContext<FeedCtx>({ items: null, loading: true, error: null, reload: () => undefined });
export const useFeed = () => useContext(FeedContext);

export function FeedProvider({ children }: { children: ReactNode }) {
  const { company, role, feedVersion } = useApp();
  const { data, loading, error, reload } = useAsync(
    () => (company && role ? api.getFeed(company.company_id, role) : Promise.resolve([] as FeedItem[])),
    [company?.company_id, role, feedVersion],
  );
  return <FeedContext.Provider value={{ items: data, loading, error, reload }}>{children}</FeedContext.Provider>;
}

/* ---------------- Переключатели компании и роли ---------------- */

function CompanySwitcher() {
  const { companies, company, role, setCompany } = useApp();
  const { open, setOpen, ref } = usePopover();
  if (!company) return null;
  return (
    <div className="switcher" ref={ref}>
      <button type="button" className="switcher__btn" aria-haspopup="listbox" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className="tile tile--dark" aria-hidden>{initialsOf(company)}</span>
        <span className="switcher__text">
          <span className="switcher__name">{company.name}</span>
          <span className="switcher__sub">{company.industry}{role ? ` · ${roleLabel(role)}` : ''}</span>
        </span>
        <ChevronDown size={20} aria-hidden />
      </button>
      {open && (
        <div className="popover popover--left" role="listbox" aria-label="Компания">
          <p className="popover__title">Компания</p>
          {companies.map((c) => (
            <button
              type="button"
              role="option"
              aria-selected={c.company_id === company.company_id}
              key={c.company_id}
              className="option"
              onClick={() => { setCompany(c.company_id); setOpen(false); }}
            >
              <span className="tile tile--dark" aria-hidden>{initialsOf(c)}</span>
              <span className="option__text">
                <span className="option__name">{c.name}</span>
                <span className="option__sub">{c.industry} · {c.revenue_bn_rub.toLocaleString('ru-RU')} млрд ₽</span>
              </span>
              {c.company_id === company.company_id && <Check size={20} aria-hidden />}
            </button>
          ))}
          <a className="option option--link" href={href.select} onClick={() => setOpen(false)}>Выбрать на стартовом экране</a>
        </div>
      )}
    </div>
  );
}

function RoleSwitcher() {
  const { role, setRole } = useApp();
  const { open, setOpen, ref } = usePopover();
  if (!role) return null;
  return (
    <div className="switcher switcher--role" ref={ref}>
      <button type="button" className="switcher__btn switcher__btn--role" aria-haspopup="listbox" aria-expanded={open} onClick={() => setOpen(!open)}>
        <User size={18} aria-hidden />
        <span className="switcher__text">
          <span className="switcher__sub">Роль</span>
          <span className="switcher__name">{roleLabel(role)}</span>
        </span>
        <ChevronDown size={20} aria-hidden />
      </button>
      {open && (
        <div className="popover popover--left" role="listbox" aria-label="Роль пользователя">
          <p className="popover__title">Роль пользователя</p>
          {ROLES.map((r) => (
            <button type="button" role="option" aria-selected={r.id === role} key={r.id} className="option" onClick={() => { setRole(r.id); setOpen(false); }}>
              <span className="option__text">
                <span className="option__name">{r.label}</span>
                <span className="option__sub">{r.hint}</span>
              </span>
              {r.id === role && <Check size={20} aria-hidden />}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/* ---------------- Демо-панель ---------------- */

function DemoControls() {
  const { demo, updateDemo, bumpFeed } = useApp();
  const { open, setOpen, ref } = usePopover();
  return (
    <div className="switcher" ref={ref}>
      <IconButton label="Демо-режим" aria-expanded={open} aria-haspopup="dialog" onClick={() => setOpen(!open)}>
        <FlaskConical size={22} />
      </IconButton>
      {open && (
        <div className="popover popover--right demo-pop" role="dialog" aria-label="Демо-режим">
          <p className="popover__title">Демо-режим</p>
          <p className="muted small">Имитация состояний, которые трудно показать вживую. Работает в режиме mock.</p>
          <fieldset className="demo-field">
            <legend className="small medium">Задержка ответа</legend>
            <div className="segmented" role="radiogroup" aria-label="Задержка ответа">
              {(['fast', 'normal', 'slow'] as const).map((l) => (
                <button type="button" role="radio" aria-checked={demo.latency === l} key={l} className="segmented__item" onClick={() => updateDemo({ latency: l })}>
                  {{ fast: 'Быстро', normal: 'Обычно', slow: 'Медленно' }[l]}
                </button>
              ))}
            </div>
          </fieldset>
          <label className="switch-row">
            <input type="checkbox" checked={demo.budgetExhausted} onChange={(e) => { updateDemo({ budgetExhausted: e.target.checked }); bumpFeed(); }} />
            <span>Бюджет LLM исчерпан (ответы «сохранённые»)</span>
          </label>
          <div className="stack gap-8">
            <Button size="s" block onClick={() => { updateDemo({ failNext: true }); setOpen(false); bumpFeed(); }}>Ошибка при следующем запросе</Button>
            <Button size="s" block onClick={() => { resetDemoNews(); bumpFeed(); setOpen(false); }}>Удалить загруженные новости</Button>
          </div>
        </div>
      )}
    </div>
  );
}

/* ---------------- Шапка ---------------- */

export function Header() {
  const { theme, toggleTheme, demo } = useApp();
  return (
    <header className="app-header">
      <a className="brand" href={href.home} aria-label="Альфа-Бизнес, главная">
        <span className="brand__mark" aria-hidden />
        <span className="brand__name">Альфа-Бизнес</span>
        <Badge tone="warning">Демо</Badge>
      </a>
      <div className="app-header__switchers">
        <CompanySwitcher />
        <RoleSwitcher />
      </div>
      <div className="app-header__tools">
        {demo.budgetExhausted && <Badge tone="warning">Бюджет LLM исчерпан</Badge>}
        <IconButton label="Поиск" className="hide-m"><Search size={22} /></IconButton>
        <IconButton label="Уведомления" className="hide-m"><Bell size={22} /></IconButton>
        <DemoControls />
        <IconButton label={theme === 'light' ? 'Тёмная тема' : 'Светлая тема'} onClick={toggleTheme}>
          {theme === 'light' ? <Moon size={22} /> : <Sun size={22} />}
        </IconButton>
        <span className="avatar" aria-label="Профиль пользователя">ИП</span>
      </div>
    </header>
  );
}

/* ---------------- Навигация ---------------- */

function useHighCount() {
  const { items } = useFeed();
  return items ? items.filter((i) => i.relevance.level === 'high').length : 0;
}

const DUMMY = [
  { label: 'Счета', icon: Wallet },
  { label: 'Платежи', icon: ArrowLeftRight },
  { label: 'Документы', icon: FileText },
];

export function Sidebar({ route }: { route: Route }) {
  const high = useHighCount();
  const newsActive = route.name === 'feed' || route.name === 'event';
  return (
    <nav className="sidebar" aria-label="Основное меню">
      <a className="nav-item" href={href.home} aria-current={route.name === 'home' ? 'page' : undefined}>
        <Home size={24} aria-hidden /><span className="nav-item__label">Главная</span>
      </a>
      {DUMMY.slice(0, 2).map(({ label, icon: Icon }) => (
        <span className="nav-item nav-item--dummy" key={label} title="Раздел вне демо" aria-disabled="true">
          <Icon size={24} aria-hidden /><span className="nav-item__label">{label}</span>
        </span>
      ))}
      <a className="nav-item" href={href.feed} aria-current={newsActive ? 'page' : undefined}>
        <Newspaper size={24} aria-hidden />
        <span className="nav-item__label">Новости и аналитика</span>
        {high > 0 && <span className="count" aria-label={`${high} важных событий`}>{high}</span>}
      </a>
      {DUMMY.slice(2).map(({ label, icon: Icon }) => (
        <span className="nav-item nav-item--dummy" key={label} title="Раздел вне демо" aria-disabled="true">
          <Icon size={24} aria-hidden /><span className="nav-item__label">{label}</span>
        </span>
      ))}
    </nav>
  );
}

export function BottomNav({ route }: { route: Route }) {
  const high = useHighCount();
  const newsActive = route.name === 'feed' || route.name === 'event';
  return (
    <nav className="bottom-nav" aria-label="Основное меню">
      <a href={href.home} aria-current={route.name === 'home' ? 'page' : undefined}><Home size={24} aria-hidden /><span>Главная</span></a>
      <span aria-disabled="true"><ArrowLeftRight size={24} aria-hidden /><span>Платежи</span></span>
      <a href={href.feed} aria-current={newsActive ? 'page' : undefined} className="bottom-nav__news">
        <Newspaper size={24} aria-hidden /><span>Новости</span>
        {high > 0 && <span className="count" aria-label={`${high} важных событий`}>{high}</span>}
      </a>
      <button type="button" onClick={() => navigate(href.select)}><MoreHorizontal size={24} aria-hidden /><span>Ещё</span></button>
    </nav>
  );
}
