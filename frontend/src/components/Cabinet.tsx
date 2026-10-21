import { useState, type ReactNode } from 'react';
import {
  ArrowDown, ArrowLeftRight, ArrowUp, Briefcase, Calendar, Check, ChevronDown, ChevronRight, ChevronUp, Copy, FileInput, FilePlus,
  FileSpreadsheet, FileText, Globe, Home, Landmark, Mail, MoreHorizontal, Newspaper, Pencil, Percent, Plus, 
  Rocket, Search, ShoppingCart, Sparkles, Users, Wallet, Moon, Sun,
} from 'lucide-react';
import { ROLES, roleLabel } from '../data/companies';
import { href, navigate, type Route } from '../router';
import { useApp } from '../store';
import { Disclaimer } from './EventBlocks';
import { NewsWidget } from './NewsWidget';
import { DemoControls, NotificationsBell, UserMenu, useHighCount } from './Shell';
import { IconButton, Pill, usePopover } from './ui';

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
        <Dummy icon={Rocket} label="Лента операций" />
        <Dummy icon={ArrowLeftRight} label="Платежи в работе" />
        <Dummy icon={FileInput} label="Реестры платежей" />
        <Dummy icon={FileText} label="Выписка" />
        <Dummy icon={Wallet} label="Счета" />
        <Dummy icon={Users} label="Контрагенты" />

        <p className="cab-nav__group">Мои сервисы <Plus size={16} aria-hidden /></p>
        <a className="cab-nav__item" href={href.feed} aria-current={newsActive ? 'page' : undefined}>
          <Newspaper size={22} aria-hidden /><span className="cab-nav__label">Новости и аналитика</span>
          {high > 0 && <span className="count" aria-label={`${high} важных событий`}>{high}</span>}
        </a>
        <Dummy icon={Percent} label="Кредитные продукты" />
        <Dummy icon={Landmark} label="Депозиты" tag="НОВОЕ" />
        <Dummy icon={Globe} label="ВЭД" />
      </nav>
      <div className="cab-side__bottom">
        <div className="cab-tiles">
          <span className="cab-tile" title={OUT} aria-disabled="true"><ShoppingCart size={18} aria-hidden />Все сервисы и продукты</span>
          <span className="cab-tile" title={OUT} aria-disabled="true"><Sparkles size={18} aria-hidden />Кэшбэк и привилегии</span>
        </div>
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
  const { theme, toggleTheme, demo } = useApp();
  return (
    <header className="cab-top">
      <a className="cab-top__logo" href={href.home} aria-label="Альфа-Бизнес, главная"><span className="brand__mark" aria-hidden /></a>
      <div className="cab-top__tools">
        {demo.budgetExhausted && <Pill tone="warn">Бюджет LLM исчерпан</Pill>}
        <span className="cab-search" title={OUT}><Search size={20} aria-hidden /><span>Поиск</span></span>
        <span className="cab-top__sep" aria-hidden />
        <IconButton label="Календарь" className="cab-top__dummy hide-m" disabled><Calendar size={22} /></IconButton>
        <IconButton label="Сообщения" className="cab-top__dummy hide-m" disabled><Mail size={22} /></IconButton>
        <NotificationsBell />
        <span className="cab-top__sep hide-m" aria-hidden />
        <CabAccount />
        <DemoControls />
        <IconButton label={theme === 'light' ? 'Тёмная тема' : 'Светлая тема'} className="cab-top__theme" onClick={toggleTheme}>
          {theme === 'light' ? <Moon size={22} /> : <Sun size={22} />}
        </IconButton>
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
  { icon: Globe, label: 'Предоставить сведения о ВО' },
];

const STATUSES: [string, number, boolean?][] = [
  ['Оплатить', 1, true], ['Подписать', 7], ['Отправить', 7], ['Доработать', 4], ['Отклонённые', 7], ['Черновики', 5], ['В работе у банка', 8],
];

const OPS = [
  { name: 'ООО «ЭнергоСбыт»', doc: 'счёт 2026/417', amount: '−18 240 000,00', out: true },
  { name: 'АО «ЛогистикПро»', doc: 'акт № 88', amount: '−4 905 300,00', out: true },
  { name: 'ООО «ТоргСнаб»', doc: 'оплата по договору 12/Д', amount: '+27 600 000,00', out: false },
  { name: 'АО «МеталлТрейд»', doc: 'оплата по счёту 311', amount: '+9 150 000,00', out: false },
];

function MoneySummary({ label, value, dir }: { label: string; value: string; dir?: 'up' | 'down' }) {
  return (
    <div className="cab-sum">
      <p className="cab-sum__label">
        {label}
        {dir === 'up' && <ArrowUp size={14} className="cab-sum__up" aria-hidden />}
        {dir === 'down' && <ArrowDown size={14} className="cab-sum__down" aria-hidden />}
      </p>
      <p className="cab-sum__value num">{value} ₽</p>
    </div>
  );
}

/** Главный экран кабинета (имитация). Новости и аналитика — заметный блок справа, как рекламные плитки в банке. */
export function CabinetHome() {
  const { company } = useApp();
  const [opsOpen, setOpsOpen] = useState(true);
  return (
    <div className="cab-home">
      <header className="cab-head">
        <h1 className="cab-title">Главная</h1>
        <p className="cab-sub">Компания: <strong>{company?.name}</strong> · 20 октября 2026 <Pill tone="warn">демо-данные</Pill></p>
      </header>

      <div className="cab-home__main">
        <ul className="cab-quick" aria-label="Быстрые действия">
          {QUICK.map(({ icon: Icon, label }) => (
            <li key={label}><span className="cab-quick__item" title={OUT} aria-disabled="true"><span className="cab-quick__icon" aria-hidden><Icon size={20} /></span>{label}</span></li>
          ))}
        </ul>

        <ul className="cab-status" aria-label="Платежи по статусам">
          {STATUSES.map(([label, n, hot]) => (
            <li key={label}><span title={OUT} aria-disabled="true">{label}<b className={hot ? 'is-hot' : undefined}>{n}</b></span></li>
          ))}
        </ul>

        <section className="cab-card cab-account-card" aria-label="Счета">
          <div className="cab-account-card__main">
            <p className="cab-muted">Расчётный счёт <Pencil size={14} aria-hidden /></p>
            <p className="cab-money num">412 380 940<span>,12 ₽</span></p>
            <p className="cab-muted num">40702 810 3 00000 004821 <Copy size={13} aria-hidden /></p>
            <p className="cab-own">Свои деньги: <b className="num">312 380 940,12 ₽</b></p>
            <span className="cab-btn" title={OUT} aria-disabled="true">Скопировать реквизиты</span>
          </div>
          <div className="cab-account-card__side">
            <div>
              <p className="cab-muted">Валютный счёт ··1190</p>
              <p className="cab-money cab-money--s num">3 204 118,50 $</p>
            </div>
            <div>
              <p className="cab-muted">Накопительный счёт ··7733</p>
              <p className="cab-money cab-money--s num">150 000 000,00 ₽</p>
            </div>
            <p className="cab-tariff">Тариф «Удачный выбор»</p>
          </div>
        </section>

        <section className="cab-card cab-ops" aria-labelledby="ops-title">
          <div className="cab-ops__head">
            <h2 id="ops-title" className="cab-h2">История операций</h2>
            <button type="button" className="cab-ops__toggle" aria-expanded={opsOpen} aria-label={opsOpen ? 'Свернуть' : 'Развернуть'} onClick={() => setOpsOpen(!opsOpen)}>
              {opsOpen ? <ChevronUp size={22} aria-hidden /> : <ChevronDown size={22} aria-hidden />}
            </button>
          </div>
          {opsOpen && (
            <>
              <div className="cab-ops__sums">
                <MoneySummary label="Входящий остаток" value="388 995 240,12" />
                <MoneySummary label="Поступления" value="36 750 000,00" dir="up" />
                <MoneySummary label="Списания" value="23 145 300,00" dir="down" />
                <MoneySummary label="Исходящий остаток" value="412 380 940,12" />
              </div>
              <ul className="cab-ops__list">
                {OPS.map((o) => (
                  <li key={o.doc}>
                    <span className={`cab-ops__dir${o.out ? ' is-out' : ' is-in'}`} aria-hidden>{o.out ? <ArrowUp size={16} /> : <ArrowDown size={16} />}</span>
                    <span className="cab-ops__name">{o.name} <span className="cab-muted">· {o.doc}</span></span>
                    <span className="cab-ops__amount num">{o.amount} ₽</span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </section>
      </div>

      <aside className="cab-home__aside" aria-label="Для вас">
        <NewsWidget />
        <div className="cab-promo cab-promo--blue">
          <p className="cab-promo__title">Закажите карту</p>
          <p className="cab-promo__text">До 5% кэшбэк. Карта будет доступна уже через 60 секунд</p>
          <ChevronRight className="cab-promo__chev" size={22} aria-hidden />
        </div>
        <div className="cab-promo cab-promo--peach">
          <p className="cab-promo__title">Деньги сверху</p>
          <p className="cab-promo__text">Получайте бонусные баллы за привычные операции</p>
          <ChevronRight className="cab-promo__chev" size={22} aria-hidden />
        </div>
      </aside>
    </div>
  );
}
