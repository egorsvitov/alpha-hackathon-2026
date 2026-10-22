import { useId, useState, type ReactNode } from 'react';
import {
  ArrowDown, ArrowLeftRight, ArrowRight, ArrowUp, Check, ChevronDown, ChevronRight, CirclePlus, ClipboardList, Clock, CornerDownRight, CreditCard,
  Ellipsis, Eye, EyeOff, FileCheck, FileDown, FileInput, FilePlus, FileText, House, Landmark, LayoutGrid, Mail, Moon, Newspaper, Plus,
  ReceiptText, ScrollText, Settings, ShieldCheck, Sun, Users, Wallet, X,
} from 'lucide-react';
import { ROLES, roleLabel } from '../data/companies';
import { href, navigate, type Route } from '../router';
import { useApp } from '../store';
import { AlfaLogo, BrandLockup, CompanyLogo } from './Brand';
import { Disclaimer } from './EventBlocks';
import { plural } from './FeedCard';
import { DemoControls, NotificationsBell, UserMenu, useFeed, useHighCount } from './Shell';
import { ErrorState, IconButton, Pill, SignalMark, Skeleton, usePopover } from './ui';

/* Оформление «Кабинет» по официальным скриншотам главной Альфа-Бизнеса (22.10.2026): меню слева, справа в шапке
   письма, колокольчик, компания и тёмная «пилюля» пользователя; на главной — быстрые действия, тёмная карточка
   с балансом и делами, ниже вкладки сводки. Версия облегчена: меньше равных по весу блоков, больше воздуха.
   «Новости и аналитика» — дополнительный сервис: пункт меню со счётчиком, карточка в сводке и колокольчик.
   Разделы банка вне демо показаны серыми и не работают. Все суммы и счётчики — демонстрационные. */

const OUT = 'Раздел вне демо';
type Icon = typeof House;

/* ---------------- Меню слева ---------------- */

const MENU: { icon: Icon; label: string; tag?: string }[] = [
  { icon: ClipboardList, label: 'Заявки', tag: 'НОВОЕ' },
  { icon: Plus, label: 'Новый платёж' },
  { icon: ScrollText, label: 'Лента операций' },
  { icon: Clock, label: 'Платежи в работе' },
  { icon: FileInput, label: 'Импорт реестров' },
  { icon: FileText, label: 'Выписка' },
  { icon: Wallet, label: 'Счета' },
  { icon: Users, label: 'Контрагенты' },
  { icon: FileCheck, label: 'Справки' },
  { icon: ReceiptText, label: 'Счета на оплату' },
  { icon: CreditCard, label: 'Карты' },
];

function Dummy({ icon: I, label, tag }: { icon: Icon; label: string; tag?: string }) {
  return (
    <span className="cab-nav__item cab-nav__item--dummy" title={`${label}: ${OUT.toLowerCase()}`} aria-disabled="true">
      <I size={20} aria-hidden /><span className="cab-nav__label">{label}</span>
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
        <BrandLockup size={40} />
      </a>
      <nav className="cab-nav" aria-label="Основное меню">
        <Dummy icon={LayoutGrid} label="Все сервисы" />
        <hr className="cab-nav__sep" />
        <a className="cab-nav__item" href={href.home} aria-current={route.name === 'home' ? 'page' : undefined} title="Главная">
          <House size={20} aria-hidden /><span className="cab-nav__label">Главная</span>
        </a>
        {MENU.map((m) => <Dummy key={m.label} icon={m.icon} label={m.label} tag={m.tag} />)}
        <hr className="cab-nav__sep" />
        <a className="cab-nav__item cab-nav__item--news" href={href.feed} aria-current={newsActive ? 'page' : undefined} title="Новости и аналитика">
          <Newspaper size={20} aria-hidden /><span className="cab-nav__label">Новости и аналитика</span>
          {high > 0 && <span className="count" aria-label={`${high} ${plural(high, 'важное событие', 'важных события', 'важных событий')}`}>{high}</span>}
        </a>
        <Dummy icon={Landmark} label="Госзакупки" />
      </nav>
      <div className="cab-side__bottom">
        <Dummy icon={Settings} label="Настроить меню" />
        <button type="button" className="cab-side__prev" onClick={() => setDesign('classic')}>Классическое оформление</button>
      </div>
    </aside>
  );
}

/* ---------------- Шапка ---------------- */

/** Чип компании, как «ООО … / ФИО» в шапке банка: логотип-плитка, название, сотрудник. Внутри — смена компании и роли. */
function CabAccount() {
  const { user, companies, company, role, setCompany, setRole } = useApp();
  const { open, setOpen, ref } = usePopover();
  if (!company) return null;
  const short = user ? (() => { const [f, m, l] = user.name.split(' '); return `${l ?? f} ${f[0]}.${m ? ` ${m[0]}.` : ''}`; })() : '';
  return (
    <div className="switcher cab-account" ref={ref}>
      <button type="button" className="cab-account__btn" aria-haspopup="dialog" aria-expanded={open} aria-label={`Компания: ${company.name}, ${role ? roleLabel(role) : ''}. Сменить компанию или роль`} onClick={() => setOpen(!open)}>
        <CompanyLogo company={company} size={36} />
        <span className="cab-account__text">
          <span className="cab-account__name">{company.name}</span>
          <span className="cab-account__sub">{short}{role ? ` · ${roleLabel(role)}` : ''}</span>
        </span>
        <ChevronDown size={18} aria-hidden className="cab-account__chev" />
      </button>
      {open && (
        <div className="popover popover--right" role="dialog" aria-label="Компания и роль">
          <p className="popover__title">Компания</p>
          {companies.map((c) => (
            <button type="button" key={c.company_id} className="option" aria-pressed={c.company_id === company.company_id} onClick={() => { setCompany(c.company_id); setOpen(false); }}>
              <CompanyLogo company={c} size={36} />
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
  const { demo, theme, toggleTheme } = useApp();
  return (
    <header className="cab-top">
      <a className="cab-top__logo" href={href.home} aria-label="Альфа-Бизнес, главная"><AlfaLogo size={36} /></a>
      <div className="cab-top__tools">
        {demo.budgetExhausted && <Pill tone="warn">Бюджет LLM исчерпан</Pill>}
        <DemoControls />
        <span className="icon-btn cab-top__dummy" title={`Письма: ${OUT.toLowerCase()}`} aria-disabled="true"><Mail size={20} aria-hidden /></span>
        <NotificationsBell />
        <CabAccount />
        <UserMenu variant="pill" />
        <IconButton className="cab-theme" label={theme === 'light' ? 'Тёмная тема' : 'Светлая тема'} onClick={toggleTheme}>
          {theme === 'light' ? <Moon size={20} /> : <Sun size={20} />}
        </IconButton>
      </div>
    </header>
  );
}

/** Полоса сервиса над лентой и карточкой события: хлебные крошки и пометка, что сервис встроен в банк. */
function ServiceBar({ route }: { route: Route }) {
  return (
    <div className="cab-service">
      <nav className="cab-crumbs" aria-label="Хлебные крошки">
        <a href={href.home}>Главная</a>
        <ChevronRight size={14} aria-hidden />
        {route.name === 'event'
          ? <><a href={href.feed}>Новости и аналитика</a><ChevronRight size={14} aria-hidden /><span aria-current="page">Событие</span></>
          : <span aria-current="page">Новости и аналитика</span>}
      </nav>
      <span className="cab-service__tag"><AlfaLogo size={16} />Сервис в составе Альфа-Бизнес · прототип</span>
    </div>
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
          {newsActive && <ServiceBar route={route} />}
          {children}
          <Disclaimer />
        </main>
      </div>
      <nav className="bottom-nav" aria-label="Основное меню">
        <a href={href.home} aria-current={route.name === 'home' ? 'page' : undefined}><House size={24} aria-hidden /><span>Главная</span></a>
        <span aria-disabled="true" title={OUT}><ArrowLeftRight size={24} aria-hidden /><span>Платежи</span></span>
        <a href={href.feed} aria-current={newsActive ? 'page' : undefined} className="bottom-nav__news">
          <Newspaper size={24} aria-hidden /><span>Новости</span>
          {high > 0 && <span className="count" aria-label={`${high} важных событий`}>{high}</span>}
        </a>
        <button type="button" onClick={() => navigate(href.select)}><Ellipsis size={24} aria-hidden /><span>Ещё</span></button>
      </nav>
    </div>
  );
}

/* ---------------- Главная: демонстрационные данные ---------------- */

const QUICK: { icon: Icon; label: string }[] = [
  { icon: FileDown, label: 'Скачать выписку' },
  { icon: FileInput, label: 'Импорт платежей' },
  { icon: FilePlus, label: 'Создать платёж' },
];

const DEALS: { label: string; n: number; hot?: boolean }[] = [
  { label: 'Подписать и отправить', n: 2, hot: true },
  { label: 'В работе у банка', n: 5 },
  { label: 'Отклонённые', n: 1, hot: true },
  { label: 'Черновики', n: 3 },
  { label: 'Оплатить', n: 1 },
  { label: 'Принять предложение', n: 1 },
];

const REQUESTS = [
  { name: 'Лизинг', sum: '75 000 000 ₽', status: 'на подпись', tone: 'blue', date: '09.10.2026', step: 2 },
  { name: 'Банковская гарантия', sum: '18 500 000 ₽', status: 'на рассмотрении', tone: 'gray', date: '14.10.2026', step: 3 },
  { name: 'Факторинг', sum: '120 000 000 ₽', status: 'одобрено', tone: 'green', date: '17.10.2026', step: 4 },
] as const;

const ACCOUNTS = [
  { name: 'Расчётный счёт', tail: '4821', amount: '412 380 940,12', cur: 'RUB' },
  { name: 'Расчётный счёт', tail: '5307', amount: '58 214 006,40', cur: 'RUB' },
  { name: 'Валютный счёт', tail: '1190', amount: '3 204 118,50', cur: 'USD' },
  { name: 'Валютный счёт', tail: '2264', amount: '12 750 000,00', cur: 'CNY' },
  { name: 'Накопительный счёт', tail: '7733', amount: '150 000 000,00', cur: 'RUB' },
];

const OPS = [
  { name: 'ООО «ЭнергоСбыт»', doc: 'Счёт 2026/417 за электроэнергию', date: 'Сегодня', amount: '−18 240 000,00', out: true },
  { name: 'ООО «ТоргСнаб»', doc: 'Оплата по договору 12/Д', date: 'Сегодня', amount: '+27 600 000,00', out: false },
  { name: 'АО «ЛогистикПро»', doc: 'Акт № 88, перевозка', date: 'Вчера', amount: '−4 905 300,00', out: true },
  { name: 'АО «МеталлТрейд»', doc: 'Оплата по счёту 311', date: 'Вчера', amount: '+9 150 000,00', out: false },
];

const MASK = '••• ••• •••';

/* ---------------- Главная: блоки ---------------- */

function Dim({ masked, children }: { masked: boolean; children: ReactNode }) {
  return masked ? <span aria-label="Сумма скрыта">{MASK}</span> : <>{children}</>;
}

/** Арт промо-плитки «Умная карта для всех»: собственный SVG, без картинок банка. */
function SmartCardArt() {
  const id = useId().replace(/:/g, '');
  return (
    <svg className="cab-promo__art" viewBox="0 0 240 170" aria-hidden focusable="false">
      <defs>
        <radialGradient id={`${id}g`} cx="50%" cy="50%" r="50%">
          <stop offset="0" stopColor="#ef3124" stopOpacity=".85" />
          <stop offset="1" stopColor="#ef3124" stopOpacity="0" />
        </radialGradient>
        <linearGradient id={`${id}c`} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#f6f7f9" />
          <stop offset=".55" stopColor="#b9bdc4" />
          <stop offset="1" stopColor="#6b7078" />
        </linearGradient>
        <linearGradient id={`${id}e`} x1="0" y1="0" x2="1" y2="0">
          <stop offset="0" stopColor="#ff5a45" />
          <stop offset="1" stopColor="#9b0e04" />
        </linearGradient>
        <linearGradient id={`${id}h`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#ffffff" stopOpacity=".85" />
          <stop offset="1" stopColor="#c9ccd2" stopOpacity=".55" />
        </linearGradient>
      </defs>
      <ellipse cx="140" cy="146" rx="104" ry="22" fill={`url(#${id}g)`} />
      <g transform="translate(58 46) rotate(-14) skewX(10)">
        <rect x="0" y="10" width="168" height="100" rx="14" fill={`url(#${id}e)`} />
        <rect x="0" y="0" width="168" height="100" rx="14" fill={`url(#${id}c)`} />
        <rect x="18" y="20" width="26" height="19" rx="4" fill="#d6b25e" />
        <path d="M18 29.5h26M31 20v19" stroke="#a8863c" strokeWidth="1.2" />
        <rect x="140" y="22" width="14" height="5" rx="2.5" fill="#fff" opacity=".9" />
        <rect x="140" y="31" width="14" height="5" rx="2.5" fill="#fff" opacity=".9" />
        <text x="18" y="84" fontSize="12" fontFamily="ui-monospace, Consolas, monospace" fill="#2fd27b" letterSpacing="2">1010 0 1010 0</text>
      </g>
      <g transform="translate(64 50)">
        <path d="M0 -26 22.5 -13v26L0 26l-22.5-13v-26z" fill={`url(#${id}h)`} stroke="#fff" strokeOpacity=".8" strokeWidth="1.2" />
        <circle cx="0" cy="-6" r="6.5" fill="#8d929a" />
        <path d="M-11 12c1.5-7 6-10 11-10s9.5 3 11 10z" fill="#8d929a" />
      </g>
    </svg>
  );
}

function Hero({ masked }: { masked: boolean }) {
  const { company } = useApp();
  const [offer, setOffer] = useState(true);
  return (
    <section className="cab-hero" aria-label="Счета и дела компании">
      <div className="cab-hero__main">
        <div className="cab-bal">
          <p className="cab-bal__label">Остаток на счетах · {company?.name}</p>
          <p className="cab-bal__sum num"><Dim masked={masked}>412 380 940,12 <span>RUB</span></Dim></p>
          <p className="cab-bal__over"><CornerDownRight size={16} aria-hidden /><Dim masked={masked}><span className="num">462 380 940,12 RUB</span></Dim> с овердрафтом</p>
          <p className="cab-bal__cur">
            <span className="num"><Dim masked={masked}>3 204 118,50 USD</Dim></span>
            <i aria-hidden>·</i>
            <span className="num"><Dim masked={masked}>12 750 000,00 CNY</Dim></span>
            <span className="cab-bal__more" title="Ещё 2 валютных остатка">+2</span>
          </p>
          {offer && (
            <p className="cab-bal__offer">
              <CirclePlus size={18} aria-hidden className="cab-bal__plus" />
              <span>Переводите себе бесплатно <b className="num">5 000 000 ₽</b></span>
              <span className="cab-ok">ОДОБРЕНО</span>
              <button type="button" className="cab-bal__close" aria-label="Скрыть предложение" onClick={() => setOffer(false)}><X size={16} aria-hidden /></button>
            </p>
          )}
          <div className="cab-bal__tiles">
            <span className="cab-bal__tile" title={OUT} aria-disabled="true">
              <span className="cab-bal__ico" aria-hidden><Wallet size={20} /></span>
              <span><b>Счета</b><small>3 в рублях, 2 в валюте</small></span>
            </span>
            <span className="cab-bal__tile" title={OUT} aria-disabled="true">
              <span className="cab-bal__ico" aria-hidden><CreditCard size={20} /></span>
              <span><b>Бизнес-карты</b><small>Реквизиты и операции</small></span>
            </span>
          </div>
        </div>
        <div className="cab-hero__links">
          <span title={OUT} aria-disabled="true">Реквизиты</span>
          <span title={OUT} aria-disabled="true">Тариф</span>
        </div>
      </div>

      <div className="cab-deals">
        <h2 className="cab-hero__h">Дела в работе</h2>
        <ul>
          {DEALS.map((d) => (
            <li key={d.label} title={OUT}>
              <span>{d.label}</span>
              <b className={d.hot ? 'is-hot' : undefined}>{d.n}</b>
            </li>
          ))}
        </ul>
      </div>

      <div className="cab-promo" title={OUT}>
        <h2 className="cab-hero__h">Умная карта для всех</h2>
        <p>Кэшбэк, бонусы и удобные платежи</p>
        <SmartCardArt />
      </div>
    </section>
  );
}

function Ring({ step, of = 5 }: { step: number; of?: number }) {
  return (
    <svg className="cab-ring" viewBox="0 0 20 20" aria-hidden focusable="false">
      <circle cx="10" cy="10" r="7.5" pathLength={100} className="cab-ring__track" />
      <circle cx="10" cy="10" r="7.5" pathLength={100} className="cab-ring__val" strokeDasharray={`${(step / of) * 100} 100`} transform="rotate(-90 10 10)" />
    </svg>
  );
}

function Requests() {
  return (
    <section className="cab-card cab-req" aria-labelledby="req-title">
      <header className="cab-card__head">
        <h2 id="req-title" className="cab-h2">Активные заявки <span className="cab-n">{REQUESTS.length}</span></h2>
        <span className="cab-card__go" title={OUT} aria-disabled="true"><ChevronRight size={20} aria-hidden /></span>
      </header>
      <ul className="cab-req__list">
        {REQUESTS.map((r) => (
          <li key={r.name}>
            <p className="cab-req__name">{r.name}</p>
            <p className="cab-req__row">
              <span className="num medium">{r.sum}</span>
              <span className={`cab-status cab-status--${r.tone}`}>{r.status}</span>
            </p>
            <p className="cab-req__row cab-req__meta">
              <span>{r.date}</span>
              <span className="cab-req__step">{r.step} из 5 <Ring step={r.step} /></span>
            </p>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** «Новости и аналитика» на главной: дополнительный сервис в сводке — счётчик, 3 события, вход в ленту. */
function NewsCard() {
  const { company } = useApp();
  const { items, loading, error, reload } = useFeed();
  const top = (items ?? []).slice(0, 3);
  const high = (items ?? []).filter((i) => i.relevance.level === 'high').length;
  return (
    <section className="cab-card cab-news" aria-labelledby="cab-news-title">
      <header className="cab-card__head">
        <h2 id="cab-news-title" className="cab-h2 cab-news__h">
          <span className="cab-news__icon" aria-hidden><Newspaper size={18} /></span>
          Новости и аналитика
        </h2>
        <a className="cab-card__go" href={href.feed} aria-label="Открыть ленту новостей и аналитики"><ChevronRight size={20} aria-hidden /></a>
      </header>
      {loading && (
        <div className="cab-news__skel" aria-busy="true">
          <p className="sr-only" role="status">Загружаем события…</p>
          <Skeleton h={16} w="70%" /><Skeleton h={16} w="90%" /><Skeleton h={16} w="80%" />
        </div>
      )}
      {error && <ErrorState message={error} onRetry={reload} />}
      {!loading && !error && (
        <>
          <p className="cab-news__lead">
            {high > 0
              ? <><b className="cab-news__num">{high}</b> {plural(high, 'важное событие', 'важных события', 'важных событий')} для {company?.name}</>
              : `Важных событий для ${company?.name ?? 'компании'} нет`}
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

function RiskCard() {
  return (
    <section className="cab-card cab-risk" aria-labelledby="risk-title">
      <header className="cab-card__head">
        <h2 id="risk-title" className="cab-h2">Индикатор риска</h2>
        <span className="cab-card__go" title={OUT} aria-disabled="true"><ChevronRight size={20} aria-hidden /></span>
      </header>
      <div className="cab-risk__gauge">
        <svg className="cab-risk__arc" viewBox="0 0 200 112" aria-hidden focusable="false">
          <path d="M16 100a84 84 0 0 1 168 0" pathLength={100} className="cab-risk__track" />
          <path d="M16 100a84 84 0 0 1 168 0" pathLength={100} className="cab-risk__val" strokeDasharray="24 100" />
        </svg>
        <div className="cab-risk__text">
          <ShieldCheck size={22} aria-hidden />
          <p className="cab-risk__level">Низкий риск</p>
        </div>
      </div>
      <p className="cab-risk__sub">Действия компании и контрагентов</p>
      <p className="cab-risk__note">Блокировок по счетам и требований ФНС нет. Обновлено сегодня в 09:40.</p>
    </section>
  );
}

type OpFilter = 'all' | 'in' | 'out';

function Operations({ masked }: { masked: boolean }) {
  const [dir, setDir] = useState<OpFilter>('all');
  const rows = OPS.filter((o) => dir === 'all' || (dir === 'out') === o.out);
  return (
    <section className="cab-card cab-ops" aria-labelledby="ops-title">
      <header className="cab-card__head">
        <h2 id="ops-title" className="cab-h2">Последние операции</h2>
        <span className="cab-link-dummy" title={OUT} aria-disabled="true">Лента операций</span>
      </header>
      <div className="cab-ops__filters" role="group" aria-label="Направление платежа">
        {([['all', 'Все'], ['in', 'Входящие'], ['out', 'Исходящие']] as const).map(([id, label]) => (
          <button type="button" key={id} className="cab-chip" aria-pressed={dir === id} onClick={() => setDir(id)}>{label}</button>
        ))}
      </div>
      <ul className="cab-oplist">
        {rows.map((o) => (
          <li key={o.doc}>
            <span className={`cab-ops__dir ${o.out ? 'is-out' : 'is-in'}`} aria-hidden>{o.out ? <ArrowUp size={16} /> : <ArrowDown size={16} />}</span>
            <span className="cab-ops__name">{o.name}<span className="cab-ops__doc">{o.doc}</span></span>
            <span className="cab-ops__date">{o.date}</span>
            <span className={`cab-ops__sum num${o.out ? '' : ' is-in'}`}><Dim masked={masked}>{o.amount} ₽</Dim></span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Accounts({ masked }: { masked: boolean }) {
  return (
    <section className="cab-card cab-accs" aria-label="Счета на главной">
      <ul>
        {ACCOUNTS.map((a) => (
          <li key={a.tail}>
            <span className="cab-accs__ico" aria-hidden>{a.cur}</span>
            <span className="cab-accs__name">{a.name}<span>··{a.tail}</span></span>
            <span className="num medium"><Dim masked={masked}>{a.amount} {a.cur}</Dim></span>
          </li>
        ))}
      </ul>
    </section>
  );
}

type HomeTab = 'summary' | 'accounts';

/** Главный экран кабинета (имитация). Основное — управление бизнесом; «Новости и аналитика» — карточка в сводке. */
export function CabinetHome() {
  const [masked, setMasked] = useState(false);
  const [tab, setTab] = useState<HomeTab>('summary');
  return (
    <div className="cab-home">
      <h1 className="sr-only cab-title">Главная</h1>

      <div className="cab-quick-row">
        <ul className="cab-quick" aria-label="Быстрые действия">
          {QUICK.map(({ icon: I, label }) => (
            <li key={label}><span className="cab-quick__btn" title={OUT} aria-disabled="true"><I size={18} aria-hidden />{label}</span></li>
          ))}
          <li><span className="cab-quick__btn cab-quick__btn--more" title={OUT} aria-disabled="true">Ещё</span></li>
        </ul>
        <Pill tone="warn">демо-данные · 20 октября 2026</Pill>
      </div>

      <Hero masked={masked} />

      <div className="cab-tabs-row">
        <div className="cab-tabs" role="tablist" aria-label="Разделы главной">
          <button type="button" role="tab" aria-selected={tab === 'summary'} className="cab-tab" onClick={() => setTab('summary')}>Сводка</button>
          <button type="button" role="tab" aria-selected={tab === 'accounts'} className="cab-tab" onClick={() => setTab('accounts')}>Счета на главной</button>
          <span className="cab-tab cab-tab--off" title={OUT} aria-disabled="true">Мультибанк</span>
          <span className="cab-tab cab-tab--off" title={OUT} aria-disabled="true">Металлы</span>
        </div>
        <div className="cab-tabs__tools">
          <button type="button" className="cab-eye" aria-pressed={masked} aria-label={masked ? 'Показать суммы' : 'Скрыть суммы'} title={masked ? 'Показать суммы' : 'Скрыть суммы'} onClick={() => setMasked(!masked)}>
            {masked ? <EyeOff size={20} aria-hidden /> : <Eye size={20} aria-hidden />}
          </button>
          <span className="icon-btn cab-top__dummy" title={`Настройка главной: ${OUT.toLowerCase()}`} aria-disabled="true"><Settings size={20} aria-hidden /></span>
        </div>
      </div>

      {tab === 'summary' ? (
        <div className="cab-summary" role="tabpanel" aria-label="Сводка">
          <Requests />
          <NewsCard />
          <RiskCard />
          <div className="cab-summary__wide"><Operations masked={masked} /></div>
        </div>
      ) : (
        <div role="tabpanel" aria-label="Счета на главной"><Accounts masked={masked} /></div>
      )}
    </div>
  );
}
