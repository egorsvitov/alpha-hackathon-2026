import { useState } from 'react';
import type { Role } from '../types';
import { ROLES } from '../data/companies';
import { href, navigate } from '../router';
import { useApp } from '../store';
import { Button } from '../components/ui';

/** Экран выбора клиента и роли (в проде — результат входа в интернет-банк; здесь — демо-вход).
    Раскладка фиксирована: выбор не меняет размеры строк, страница не прыгает. */
export function SelectPage() {
  const { companies, company, role, select } = useApp();
  const [cid, setCid] = useState<string>(company?.company_id ?? companies[0].company_id);
  const [r, setR] = useState<Role>(role ?? 'cfo');

  return (
    <div className="select-page">
      <div className="select-grid">
        <header className="select-intro">
          <span className="brand__mark brand__mark--lg" aria-hidden />
          <h1 className="select-title">Новости, которые касаются именно вас</h1>
          <p className="lead muted">Выберите компанию и роль. Мы отберём события, значимые для вашего бизнеса, и объясним, почему.</p>
          <p className="small muted select-note">Демо-вход. Компании и данные синтетические; в банке выбор определяется учётной записью.</p>
        </header>

        <div className="select-form">
          <fieldset className="pick">
            <legend className="overline">1 · Компания</legend>
            <div role="radiogroup" aria-label="Компания">
              {companies.map((c) => (
                <button type="button" role="radio" aria-checked={cid === c.company_id} key={c.company_id} className="pick-row" onClick={() => setCid(c.company_id)}>
                  <span className="pick-row__dot" aria-hidden />
                  <span className="pick-row__text">
                    <span className="pick-row__name">{c.name}</span>
                    <span className="pick-row__sub">{c.industry} · {c.region}</span>
                  </span>
                  <span className="pick-row__aside num">{c.revenue_bn_rub.toLocaleString('ru-RU')} млрд ₽</span>
                </button>
              ))}
            </div>
          </fieldset>

          <fieldset className="pick">
            <legend className="overline">2 · Роль</legend>
            <div role="radiogroup" aria-label="Роль">
              {ROLES.map((x) => (
                <button type="button" role="radio" aria-checked={r === x.id} key={x.id} className="pick-row pick-row--role" onClick={() => setR(x.id)}>
                  <span className="pick-row__dot" aria-hidden />
                  <span className="pick-row__text">
                    <span className="pick-row__name">{x.label}</span>
                    <span className="pick-row__sub">{x.hint}</span>
                  </span>
                </button>
              ))}
            </div>
          </fieldset>

          <Button variant="primary" size="xl" block onClick={() => { select(cid, r); navigate(href.home); }}>Показать события</Button>
        </div>
      </div>
    </div>
  );
}
