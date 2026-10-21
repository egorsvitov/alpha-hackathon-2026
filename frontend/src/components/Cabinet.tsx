import type { ReactNode } from 'react';
import { ArrowLeftRight, Briefcase, Check, FileText, Home, MoreHorizontal, Newspaper, Plus, Search, Users, Wallet } from 'lucide-react';
import { ROLES, roleLabel } from '../data/companies';
import { href, navigate, type Route } from '../router';
import { useApp } from '../store';
import { Disclaimer } from './EventBlocks';
import { NewsWidget } from './NewsWidget';
import { DemoControls, NotificationsBell, UserMenu, useHighCount } from './Shell';
import { Pill, usePopover } from './ui';

/* Кабинет по образцу Альфа-Бизнеса: серый фон страницы, белые карточки с крупным скруглением, слева меню, сверху поиск и компания.
   Разделы вне демо показаны, но не работают (как и в первой версии). Оформление переключается в меню профиля. */

const OUT = 'Раздел вне демо';

function Dummy({ icon: Icon, label, tag }: { icon: typeof Home; label: string; tag?: string }) {
  return (
    <span className="cab-nav__item cab-nav__item--dummy" title={OUT} aria-disabled="true">
      <Icon size={22} aria-hidden /><span className="cab-nav__label">{label}</span>
      {tag && <span className="cab-tag">{tag}</span>}
    </span>
  );
}

function CabSidebar({ route }: { route: Route }) {
  const { setDesign } = useApp();
  const high = useHighCount();
  const newsActive = route.name === 'feed' || route.name === 'event';
  return (
    <aside className="cab-side">
      <a className="cab-brand" href={href.home} aria-label="Альфа-Бизнес, главная">
        <span className="brand__mark" aria-hidden />
        <span className="cab-brand__name">Альфа-Бизнес</span>
      </a>
      <nav className="cab-nav" aria-label="Основное меню">
        <span className="cab-nav__new" title={OUT} aria-disabled="true">
          <span className="cab-nav__plus" aria-hidden><Plus size={14} strokeWidth={3} /></span>
          <span className="cab-nav__label">Новый платёж</span>
        </span>
        <a className="cab-nav__item" href={href.home} aria-current={route.name === 'home' ? 'page' : undefined}>
          <Home size={22} aria-hidden /><span className="cab-nav__label">Главная</span>
        </a>
        <a className="cab-nav__item" href={href.feed} aria-current={newsActive ? 'page' : undefined}>
          <Newspaper size={22} aria-hidden /><span className="cab-nav__label">Новости и аналитика</span>
          {high > 0 && <span className="count" aria-label={`${high} важных событий`}>{high}</span>}
        </a>
        <Dummy icon={Wallet} label="Счета" />
        <Dummy icon={ArrowLeftRight} label="Платежи" />
        <Dummy icon={FileText} label="Выписка" />
        <Dummy icon={Users} label="Контрагенты" />
      </nav>
      <div className="cab-side__bottom">
        <button type="button" className="cab-side__prev" onClick={() => setDesign('classic')}>Классическое оформление</button>
      </div>
    </aside>
  );
}

/** Компания и роль одним блоком, как «ООО … / ФИО» в шапке банка. */
function CabAccount() {
  const { user, companies, company, role, setCompany, setRole } = useApp();
  const { open, setOpen, ref } = usePopover();
  if (!company) return null;
  const short = user ? user.name.split(' ').map((w, i) => (i === 0 ? w : `${w[0]}.`)).join(' ') : '';
  return (
    <div className="switcher cab-account" ref={ref}>
      <button type="button" className="cab-account__btn" aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className="cab-account__text">
          <span className="cab-account__name">{company.name}</span>
          <span className="cab-account__sub">{role ? roleLabel(role) : short}</span>
        </span>
        <Briefcase size={22} aria-hidden />
      </button>
      {open && (
        <div className="popover popover--right" role="dialog" aria-label="Компания и роль">
          <p className="popover__title">Компания</p>
          {companies.map((c) => (
            <button type="button" key={c.company_id} className="option" aria-pressed={c.company_id === company.company_id} onClick={() => { setCompany(c.company_id); setOpen(false); }}>
              <span className="option__text">
                <span className="option__name">{c.name}</span>
                <span className="option__sub">{c.industry} · {c.revenue_bn_rub.toLocaleString('ru-RU')} млрд ₽</span>
              </span>
              {c.company_id === company.company_id && <Check size={20} aria-hidden />}
            </button>
          ))}
          <p className="popover__title">Роль пользователя</p>
          {ROLES.map((r) => (
            <button type="button" key={r.id} className="option" aria-pressed={r.id === role} onClick={() => { setRole(r.id); setOpen(false); }}>
              <span className="option__text">
                <span className="option__name">{r.label}</span>
                <span className="option__sub">{r.hint}</span>
              </span>
              {r.id === role && <Check size={20} aria-hidden />}
            </button>
          ))}
          <a className="option option--link" href={href.select} onClick={() => setOpen(false)}>Выбрать на стартовом экране</a>
        </div>
      )}
    </div>
  );
}

function CabTopbar() {
  const { demo } = useApp();
  return (
    <header className="cab-top">
      <a className="cab-top__logo" href={href.home} aria-label="Альфа-Бизнес, главная"><span className="brand__mark" aria-hidden /></a>
      <div className="cab-top__tools">
        {demo.budgetExhausted && <Pill tone="warn">Бюджет LLM исчерпан</Pill>}
        <span className="cab-search" title={OUT}><Search size={20} aria-hidden /><span>Поиск</span></span>
        <span className="cab-top__sep" aria-hidden />
        <NotificationsBell />
        <CabAccount />
        <DemoControls />
        <UserMenu />
      </div>
    </header>
  );
}

export function CabinetShell({ route, children }: { route: Route; children: ReactNode }) {
  const high = useHighCount();
  const newsActive = route.name === 'feed' || route.name === 'event';
  return (
    <div className="cab">
      <a className="skip-link" href="#main">К содержимому</a>
      <CabSidebar route={route} />
      <div className="cab-body">
        <CabTopbar />
        <main id="main" className="cab-main">
          {children}
          <Disclaimer />
        </main>
      </div>
      <nav className="bottom-nav" aria-label="Основное меню">
        <a href={href.home} aria-current={route.name === 'home' ? 'page' : undefined}><Home size={24} aria-hidden /><span>Главная</span></a>
        <span aria-disabled="true"><ArrowLeftRight size={24} aria-hidden /><span>Платежи</span></span>
        <a href={href.feed} aria-current={newsActive ? 'page' : undefined} className="bottom-nav__news">
          <Newspaper size={24} aria-hidden /><span>Новости</span>
          {high > 0 && <span className="count" aria-label={`${high} важных событий`}>{high}</span>}
        </a>
        <button type="button" onClick={() => navigate(href.select)}><MoreHorizontal size={24} aria-hidden /><span>Ещё</span></button>
      </nav>
    </div>
  );
}

/* ---------------- Главная ---------------- */

const ACCOUNTS = [
  { name: 'Валютный счёт', tail: '1190', amount: '3 204 118,50 $' },
  { name: 'Накопительный счёт', tail: '7733', amount: '150 000 000,00 ₽' },
];

const TO_SIGN = [
  { name: 'ООО «ЭнергоСбыт»', doc: 'счёт 2026/417', amount: '18 240 000,00' },
  { name: 'АО «ЛогистикПро»', doc: 'акт № 88', amount: '4 905 300,00' },
];

/** Главный экран кабинета (имитация). Один главный блок — «Новости и аналитика»; остальное нейтральное и компактное. */
export function CabinetHome() {
  const { company } = useApp();
  return (
    <div className="cab-home">
      <header className="cab-head">
        <h1 className="cab-title">Главная</h1>
        <p className="cab-sub">{company?.name} · 20 октября 2026 <Pill tone="warn">демо-данные</Pill></p>
      </header>

      <div className="cab-home__main">
        <NewsWidget />
      </div>

      <aside className="cab-home__aside" aria-label="Счета и платежи">
        <section className="cab-card" aria-labelledby="cab-acc">
          <h2 id="cab-acc" className="cab-h2">Счета</h2>
          <p className="cab-muted cab-acc__label">Расчётный счёт ··4821</p>
          <p className="cab-money num">412 380 940<span>,12 ₽</span></p>
          <ul className="cab-rows">
            {ACCOUNTS.map((a) => (
              <li key={a.tail}><span>{a.name} <span className="cab-muted cab-inline">··{a.tail}</span></span><span className="num medium">{a.amount}</span></li>
            ))}
          </ul>
        </section>
        <section className="cab-card" aria-labelledby="cab-sign">
          <h2 id="cab-sign" className="cab-h2">К подписанию</h2>
          <ul className="cab-rows">
            {TO_SIGN.map((p) => (
              <li key={p.doc}>
                <span>{p.name}<span className="cab-muted cab-sub-line">{p.doc}</span></span>
                <span className="num medium">{p.amount} ₽</span>
              </li>
            ))}
          </ul>
        </section>
      </aside>
    </div>
  );
}
