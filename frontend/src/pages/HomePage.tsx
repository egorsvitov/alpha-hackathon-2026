import { ArrowDownLeft, ArrowUpRight, FileSignature, Globe, PiggyBank, Wallet } from 'lucide-react';
import { useSession } from '../store';
import { NewsWidget } from '../components/NewsWidget';
import { Button, IconTile, Pill } from '../components/ui';

const ACCOUNTS = [
  { name: 'Расчётный счёт', tail: '4821', amount: '412 380 940,12', cur: '₽', tone: 'cyan' as const, icon: <Wallet size={22} /> },
  { name: 'Валютный счёт', tail: '1190', amount: '3 204 118,50', cur: '$', tone: 'lime' as const, icon: <Globe size={22} /> },
  { name: 'Накопительный', tail: '7733', amount: '150 000 000,00', cur: '₽', tone: 'peach' as const, icon: <PiggyBank size={22} /> },
];

const PAYMENTS = [
  { name: 'ООО «ЭнергоСбыт»', doc: 'счёт 2026/417', amount: '18 240 000,00' },
  { name: 'АО «ЛогистикПро»', doc: 'акт № 88', amount: '4 905 300,00' },
];

/** Главный экран интернет-банка (имитация). Заметная точка входа — виджет «Новости и аналитика» справа. */
export function HomePage() {
  const { company } = useSession();
  return (
    <div className="home">
      <header className="home__head">
        <h1 className="home__title">Добрый день</h1>
        <p className="muted">{company?.name} · 20 октября 2026</p>
      </header>

      <section className="home__acc" aria-labelledby="acc-title">
        <div className="row between home__sec-head">
          <h2 id="acc-title" className="sec-title">Счета</h2>
          <Pill tone="warn">демо-данные</Pill>
        </div>
        <div className="acc-grid">
          {ACCOUNTS.map((a) => (
            <article key={a.tail} className={`acc-card acc-card--${a.tone}`}>
              <IconTile tone={a.tone}>{a.icon}</IconTile>
              <p className="acc-card__name">{a.name} <span>··{a.tail}</span></p>
              <p className="acc-card__amount num">{a.amount} <span>{a.cur}</span></p>
            </article>
          ))}
        </div>
        <div className="row gap-8 wrap home__actions">
          <Button size="l" variant="primary" icon={<ArrowUpRight size={18} aria-hidden />}>Платёж</Button>
          <Button size="l" icon={<ArrowDownLeft size={18} aria-hidden />}>Перевод</Button>
        </div>
      </section>

      <aside className="home__widget">
        <NewsWidget />
      </aside>

      <section className="home__pay" aria-labelledby="pay-title">
        <h2 id="pay-title" className="sec-title">К подписанию</h2>
        <ul className="pay-list">
          {PAYMENTS.map((p) => (
            <li key={p.doc}>
              <IconTile tone="neutral"><FileSignature size={20} /></IconTile>
              <span className="pay-list__name">{p.name} <span className="muted">· {p.doc}</span></span>
              <span className="pay-list__amount num">{p.amount} ₽</span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
