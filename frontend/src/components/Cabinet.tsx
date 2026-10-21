import type { ReactNode } from 'react';
import { ArrowDown, ArrowLeftRight, ArrowRight, ArrowUp, Briefcase, Check, FileInput, FilePlus, FileSpreadsheet, FileText, Home, MoreHorizontal, Newspaper, Plus, Search, Users, Wallet } from 'lucide-react';
import { ROLES, roleLabel } from '../data/companies';
import { href, navigate, type Route } from '../router';
import { useApp } from '../store';
import { Disclaimer } from './EventBlocks';
import { plural } from './FeedCard';
import { DemoControls, NotificationsBell, UserMenu, useFeed, useHighCount } from './Shell';
import { ErrorState, Pill, SignalMark, Skeleton, usePopover } from './ui';

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
        <Dummy icon={Wallet} label="Счета" />
        <Dummy icon={ArrowLeftRight} label="Платежи" />
        <Dummy icon={FileText} label="Выписка" />
        <Dummy icon={Users} label="Контрагенты" />

        <p className="cab-nav__group">Мои сервисы</p>
        <a className="cab-nav__item" href={href.feed} aria-current={newsActive ? 'page' : undefined}>
          <Newspaper size={22} aria-hidden /><span className="cab-nav__label">Новости и аналитика</span>
          {high > 0 && <span className="count" aria-label={`${high} важных событий`}>{high}</span>}
        </a>
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

const QUICK = [
  { icon: FileText, label: 'Платёж по шаблону' },
  { icon: FileSpreadsheet, label: 'Сформировать выписку' },
  { icon: FileInput, label: 'Импорт платежей из 1С' },
  { icon: FilePlus, label: 'Выставить счёт' },
];

const ACCOUNTS = [
  { name: 'Валютный счёт', tail: '1190', amount: '3 204 118,50 $' },
  { name: 'Накопительный счёт', tail: '7733', amount: '150 000 000,00 ₽' },
];

const OPS = [
  { name: 'ООО «ЭнергоСбыт»', doc: 'счёт 2026/417', amount: '−18 240 000,00', out: true },
  { name: 'ООО «ТоргСнаб»', doc: 'оплата по договору 12/Д', amount: '+27 600 000,00', out: false },
  { name: 'АО «ЛогистикПро»', doc: 'акт № 88', amount: '−4 905 300,00', out: true },
  { name: 'АО «МеталлТрейд»', doc: 'оплата по счёту 311', amount: '+9 150 000,00', out: false },
];

/** Компактный блок «Новости и аналитика»: дополнительный сервис, заметный, но не главный на экране. */
function NewsCompact() {
  const { items, loading, error, reload } = useFeed();
  const top = (items ?? []).slice(0, 3);
  const high = (items ?? []).filter((i) => i.relevance.level === 'high').length;
  return (
    <section className="cab-card cab-news" aria-labelledby="cab-news-title">
      <div className="cab-news__head">
        <span className="cab-news__icon" aria-hidden><Newspaper size={20} /></span>
        <h2 id="cab-news-title" className="cab-h2">Новости и аналитика</h2>
      </div>
      {loading && (
        <div aria-busy="true">
          <p className="sr-only" role="status">Загружаем события…</p>
          <Skeleton h={16} w="70%" /><div style={{ height: 10 }} /><Skeleton h={16} w="90%" />
        </div>
      )}
      {error && <ErrorState message={error} onRetry={reload} />}
      {!loading && !error && (
        <>
          <p className="cab-news__lead">
            {high > 0
              ? <><b className="cab-news__num">{high}</b> {plural(high, 'важное событие', 'важных события', 'важных событий')} для вашей компании</>
              : 'Важных событий нет'}
          </p>
          {top.length > 0 && (
            <ul className="cab-news__list">
              {top.map((it) => (
                <li key={it.event.event_id}>
                  <a className="cab-news__item" href={href.event(it.event.event_id)}>
                    <SignalMark level={it.relevance.level} />
                    <span className="cab-news__text">
                      <span className="cab-news__title">{it.event.title}</span>
                      <span className="cab-news__why">{it.why_short.replace(/^Связано:\s*/, '')}</span>
                    </span>
                  </a>
                </li>
              ))}
            </ul>
          )}
          <a className="cab-news__all" href={href.feed}>Все события <ArrowRight size={16} aria-hidden /></a>
        </>
      )}
    </section>
  );
}

/** Главный экран кабинета (имитация). Основное — управление бизнесом; новости — компактный блок справа. */
export function CabinetHome() {
  const { company } = useApp();
  return (
    <div className="cab-home">
      <header className="cab-head">
        <h1 className="cab-title">Главная</h1>
        <p className="cab-sub">{company?.name} · 20 октября 2026 <Pill tone="warn">демо-данные</Pill></p>
      </header>

      <div className="cab-home__main">
        <ul className="cab-quick" aria-label="Быстрые действия">
          {QUICK.map(({ icon: Icon, label }) => (
            <li key={label}><span className="cab-quick__item" title={OUT} aria-disabled="true"><span className="cab-quick__icon" aria-hidden><Icon size={20} /></span>{label}</span></li>
          ))}
        </ul>

        <section className="cab-card cab-account-card" aria-label="Счета">
          <div className="cab-account-card__main">
            <p className="cab-muted">Расчётный счёт ··4821</p>
            <p className="cab-money num">412 380 940<span>,12 ₽</span></p>
            <span className="cab-btn" title={OUT} aria-disabled="true">Скопировать реквизиты</span>
          </div>
          <ul className="cab-account-card__side cab-rows">
            {ACCOUNTS.map((a) => (
              <li key={a.tail}><span>{a.name} <span className="cab-muted cab-inline">··{a.tail}</span></span><span className="num medium">{a.amount}</span></li>
            ))}
          </ul>
        </section>

        <section className="cab-card" aria-labelledby="ops-title">
          <h2 id="ops-title" className="cab-h2">Последние операции</h2>
          <ul className="cab-rows cab-ops">
            {OPS.map((o) => (
              <li key={o.doc}>
                <span className={`cab-ops__dir${o.out ? ' is-out' : ' is-in'}`} aria-hidden>{o.out ? <ArrowUp size={16} /> : <ArrowDown size={16} />}</span>
                <span className="cab-ops__name">{o.name}<span className="cab-muted cab-sub-line">{o.doc}</span></span>
                <span className="num medium">{o.amount} ₽</span>
              </li>
            ))}
          </ul>
        </section>
      </div>

      <aside className="cab-home__aside" aria-label="Дополнительно">
        <NewsCompact />
      </aside>
    </div>
  );
}
