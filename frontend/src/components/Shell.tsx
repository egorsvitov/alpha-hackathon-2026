import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import {
  Bell, Check, ChevronDown, FileText, FlaskConical, Home, LogOut, MoreHorizontal, Moon, Newspaper, Radio, Search, Sun, User, Wallet, ArrowLeftRight, X,
} from 'lucide-react';
import { api, resetDemoNews, simulateIncoming } from '../api';
import type { FeedItem } from '../types';
import { ROLES, roleLabel } from '../data/companies';
import { BrandLockup, CompanyLogo } from './Brand';
import { href, navigate, type Route } from '../router';
import { useApp, useAsync } from '../store';
import { readEvents } from '../pages/readState';
import { Badge, Button, IconButton, usePopover } from './ui';

/* ---------------- Лента как общий ресурс (виджет, счётчик, страница ленты) ---------------- */

interface FeedCtx { items: FeedItem[] | null; loading: boolean; error: string | null; reload(): void }
const FeedContext = createContext<FeedCtx>({ items: null, loading: true, error: null, reload: () => undefined });
export const useFeed = () => useContext(FeedContext);

const canNotify = () => typeof window !== 'undefined' && 'Notification' in window;

/** Показывает браузерное уведомление, если пользователь его включил и браузер разрешил. */
function pushNotify(title: string) {
  if (!canNotify() || Notification.permission !== 'granted') return;
  try { new Notification('Новое важное событие', { body: title, tag: title }); } catch { /* не поддерживается */ }
}

export function FeedProvider({ children }: { children: ReactNode }) {
  const { company, role, feedVersion, push, hidden } = useApp();
  const { data, loading, error, reload } = useAsync(
    () => (company && role ? api.getFeed(company.company_id, role) : Promise.resolve([] as FeedItem[])),
    [company?.company_id, role, feedVersion],
  );

  /* Новое важное событие: ловим появление id, которых не было в прошлой выдаче для той же компании и роли. */
  const seen = useRef<{ key: string; ids: Set<string> } | null>(null);
  const [fresh, setFresh] = useState<FeedItem | null>(null);
  useEffect(() => {
    if (!data || !company || !role) return;
    const key = `${company.company_id}|${role}`;
    const ids = new Set(data.map((i) => i.event.event_id));
    const prev = seen.current;
    seen.current = { key, ids };
    if (!prev || prev.key !== key) return;
    const added = data.find((i) => !prev.ids.has(i.event.event_id) && i.relevance.level === 'high' && !hidden.includes(i.event.event_id));
    if (added) {
      setFresh(added);
      if (push) pushNotify(added.event.title);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data]);

  return (
    <FeedContext.Provider value={{ items: data, loading, error, reload }}>
      {children}
      {fresh && (
        <div className="toast toast--live" role="status">
          <Bell size={18} aria-hidden />
          <span>Новое важное событие: {fresh.event.title}</span>
          <a className="toast__btn" href={href.event(fresh.event.event_id)} onClick={() => setFresh(null)}>Открыть</a>
          <button type="button" className="toast__close" aria-label="Закрыть" onClick={() => setFresh(null)}><X size={16} aria-hidden /></button>
        </div>
      )}
    </FeedContext.Provider>
  );
}

/* ---------------- Уведомления ---------------- */

export function NotificationsBell() {
  const { push, setPush, hidden } = useApp();
  const { items } = useFeed();
  const { open, setOpen, ref } = usePopover();
  const [denied, setDenied] = useState(false);
  const important = (items ?? []).filter((i) => i.relevance.level === 'high' && !hidden.includes(i.event.event_id));
  const unread = important.filter((i) => !readEvents.has(i.event.event_id)).length;

  const toggle = async (on: boolean) => {
    setDenied(false);
    if (!on) { setPush(false); return; }
    if (!canNotify()) { setDenied(true); return; }
    const perm = Notification.permission === 'granted' ? 'granted' : await Notification.requestPermission();
    if (perm === 'granted') setPush(true);
    else { setPush(false); setDenied(true); }
  };

  return (
    <div className="switcher bell" ref={ref}>
      <IconButton label={unread ? `Уведомления, новых: ${unread}` : 'Уведомления'} aria-expanded={open} aria-haspopup="dialog" onClick={() => setOpen(!open)}>
        <Bell size={22} />
        {unread > 0 && <span className="bell__dot" aria-hidden />}
      </IconButton>
      {open && (
        <div className="popover popover--right" role="dialog" aria-label="Уведомления">
          <p className="popover__title">Важные события</p>
          {important.length === 0 && <p className="muted small bell__empty">Новых важных событий нет.</p>}
          <ul className="bell__list">
            {important.slice(0, 4).map((i) => (
              <li key={i.event.event_id}>
                <a className="bell__item" href={href.event(i.event.event_id)} onClick={() => setOpen(false)}>
                  <span className="medium">{i.event.title}</span>
                  <span className="small muted">{i.why_short.replace(/^Связано:\s*/, '')}</span>
                </a>
              </li>
            ))}
          </ul>
          <label className="switch-row bell__switch">
            <input type="checkbox" checked={push} onChange={(e) => void toggle(e.target.checked)} />
            <span>Присылать уведомления в браузере</span>
          </label>
          {denied && <p className="small field-error bell__note" role="alert">Браузер не разрешил уведомления. Включите их в настройках сайта и повторите.</p>}
          <p className="small muted bell__note">Придёт только то, что для вашей компании и роли имеет высокую значимость.</p>
        </div>
      )}
    </div>
  );
}

/* ---------------- Меню пользователя ---------------- */

/** Меню профиля. variant="pill" — тёмная «пилюля» с именем, как в шапке Альфа-Бизнеса (оформление «Кабинет»). */
export function UserMenu({ variant = 'avatar' }: { variant?: 'avatar' | 'pill' }) {
  const { user, logout, design, setDesign } = useApp();
  const { open, setOpen, ref } = usePopover();
  if (!user) return null;
  const initials = user.name.split(' ').map((w) => w[0]).slice(0, 2).join('');
  const [first, , last] = user.name.split(' ');
  return (
    <div className="switcher" ref={ref}>
      {variant === 'pill' ? (
        <button type="button" className="avatar--btn user-pill" aria-haspopup="dialog" aria-expanded={open} aria-label={`Профиль: ${user.name}`} onClick={() => setOpen(!open)}>
          <span className="user-pill__ava" aria-hidden>{initials}</span>
          <span className="user-pill__name">{first} {last ?? ''}</span>
        </button>
      ) : (
        <button type="button" className="avatar avatar--btn" aria-haspopup="dialog" aria-expanded={open} aria-label={`Профиль: ${user.name}`} onClick={() => setOpen(!open)}>{initials}</button>
      )}
      {open && (
        <div className="popover popover--right" role="dialog" aria-label="Профиль">
          <p className="popover__title">Вы вошли как</p>
          <p className="user-pop__name medium">{user.name}</p>
          <p className="small muted user-pop__login">Логин: {user.login}</p>
          <fieldset className="demo-field design-switch">
            <legend className="small medium">Оформление</legend>
            <div className="segmented" role="radiogroup" aria-label="Оформление">
              {([['cabinet', 'Кабинет'], ['classic', 'Классическое']] as const).map(([id, label]) => (
                <button type="button" role="radio" aria-checked={design === id} key={id} className="segmented__item" onClick={() => setDesign(id)}>{label}</button>
              ))}
            </div>
          </fieldset>
          <Button size="m" block icon={<LogOut size={18} aria-hidden />} onClick={() => { setOpen(false); logout(); navigate(href.login); }}>Выйти</Button>
        </div>
      )}
    </div>
  );
}

/* ---------------- Переключатели компании и роли ---------------- */

function CompanySwitcher() {
  const { companies, company, role, setCompany } = useApp();
  const { open, setOpen, ref } = usePopover();
  if (!company) return null;
  return (
    <div className="switcher" ref={ref}>
      <button type="button" className="switcher__btn" aria-haspopup="listbox" aria-expanded={open} onClick={() => setOpen(!open)}>
        <CompanyLogo company={company} size={32} />
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
              <CompanyLogo company={c} size={32} />
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

export function DemoControls() {
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
            <Button size="s" block icon={<Radio size={16} aria-hidden />} onClick={() => { void simulateIncoming().then(() => { bumpFeed(); setOpen(false); }); }}>Пришло новое событие</Button>
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
        <BrandLockup size={32} />
        <Badge tone="warning">Демо</Badge>
      </a>
      <div className="app-header__switchers">
        <CompanySwitcher />
        <RoleSwitcher />
      </div>
      <div className="app-header__tools">
        {demo.budgetExhausted && <Badge tone="warning">Бюджет LLM исчерпан</Badge>}
        <IconButton label="Поиск" className="hide-m"><Search size={22} /></IconButton>
        <NotificationsBell />
        <DemoControls />
        <IconButton label={theme === 'light' ? 'Тёмная тема' : 'Светлая тема'} onClick={toggleTheme}>
          {theme === 'light' ? <Moon size={22} /> : <Sun size={22} />}
        </IconButton>
        <UserMenu />
      </div>
    </header>
  );
}

/* ---------------- Навигация ---------------- */

export function useHighCount() {
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
