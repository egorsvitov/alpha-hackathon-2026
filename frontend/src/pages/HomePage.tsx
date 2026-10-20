import { ArrowDownLeft, ArrowUpRight } from 'lucide-react';
import { useApp } from '../store';
import { NewsWidget } from '../components/NewsWidget';
import { Button } from '../components/ui';

const ACCOUNTS = [
  { name: 'Расчётный счёт', tail: '4821', amount: '412 380 940,12', cur: '₽' },
  { name: 'Валютный счёт', tail: '1190', amount: '3 204 118,50', cur: '$' },
  { name: 'Накопительный счёт', tail: '7733', amount: '150 000 000,00', cur: '₽' },
];

const PAYMENTS = [
  { name: 'ООО «ЭнергоСбыт»', doc: 'счёт 2026/417', amount: '18 240 000,00' },
  { name: 'АО «ЛогистикПро»', doc: 'акт № 88', amount: '4 905 300,00' },
];

/** Главный экран интернет-банка (имитация). Заметная точка входа — виджет «Новости и аналитика» справа. */
export function HomePage() {
  const { company } = useApp();
  return (
    <div className="home">
      <header className="home__head">
        <h1 className="home__title">Добрый день</h1>
        <p className="muted">{company?.name} · 20 октября 2026 · данные счетов демонстрационные</p>
      </header>

      <section className="home__acc" aria-labelledby="acc-title">
        <h2 id="acc-title" className="section-label">Счета</h2>
        <ul className="ledger">
          {ACCOUNTS.map((a) => (
            <li key={a.tail}>
              <span className="ledger__name">{a.name} <span className="muted">··{a.tail}</span></span>
              <span className="ledger__amount num">{a.amount} <span className="ledger__cur">{a.cur}</span></span>
            </li>
          ))}
        </ul>
        <div className="row gap-8 wrap home__actions">
          <Button size="l" icon={<ArrowUpRight size={18} aria-hidden />}>Платёж</Button>
          <Button size="l" icon={<ArrowDownLeft size={18} aria-hidden />}>Перевод</Button>
        </div>
      </section>

      <aside className="home__widget">
        <NewsWidget />
      </aside>

      <section className="home__pay" aria-labelledby="pay-title">
        <h2 id="pay-title" className="section-label">К подписанию</h2>
        <ul className="ledger">
          {PAYMENTS.map((p) => (
            <li key={p.doc}>
              <span className="ledger__name">{p.name} <span className="muted">· {p.doc}</span></span>
              <span className="ledger__amount num">{p.amount} <span className="ledger__cur">₽</span></span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
