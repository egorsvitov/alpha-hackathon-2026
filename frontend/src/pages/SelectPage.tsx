import { useState } from 'react';
import { Check } from 'lucide-react';
import type { Role } from '../types';
import { ROLES } from '../data/companies';
import { href, navigate } from '../router';
import { useApp } from '../store';
import { ArtChart, CompanyArt } from '../components/Illustrations';
import { Button } from '../components/ui';

const TINTS = ['cyan', 'lime', 'peach', 'blue'];

/** Экран выбора клиента и роли (в проде — результат входа в интернет-банк; здесь — демо-вход).
    Размеры карточек фиксированы: выбор не меняет раскладку, страница не прыгает. */
export function SelectPage() {
  const { companies, company, role, select } = useApp();
  const [cid, setCid] = useState<string>(company?.company_id ?? companies[0].company_id);
  const [r, setR] = useState<Role>(role ?? 'cfo');

  return (
    <div className="select-page">
      <div className="select-grid">
        <header className="select-hero">
          <span className="brand__mark brand__mark--lg" aria-hidden />
          <h1 className="select-title">Новости, которые касаются именно вас</h1>
          <p className="select-lead">Выберите компанию и роль. Мы отберём события, значимые для вашего бизнеса, и объясним почему.</p>
          <p className="small select-note">Демо-вход. Компании и данные синтетические; в банке выбор определяется учётной записью.</p>
          <div className="select-hero__art" aria-hidden><ArtChart /></div>
        </header>

        <div className="select-form">
          <fieldset className="pick">
            <legend className="sec-title">Компания</legend>
            <div role="radiogroup" aria-label="Компания" className="pick-list">
              {companies.map((c, i) => (
                <button type="button" role="radio" aria-checked={cid === c.company_id} key={c.company_id} className={`pick-card pick-card--${TINTS[i % TINTS.length]}`} onClick={() => setCid(c.company_id)}>
                  <span className="pick-card__art" aria-hidden><CompanyArt industry={c.industry} /></span>
                  <span className="pick-card__text">
                    <span className="pick-card__name">{c.name}</span>
                    <span className="pick-card__sub">{c.industry}</span>
                    <span className="pick-card__sub">{c.region} · {c.revenue_bn_rub.toLocaleString('ru-RU')} млрд ₽</span>
                  </span>
                  <span className="pick-card__check" aria-hidden><Check size={18} /></span>
                </button>
              ))}
            </div>
          </fieldset>

          <fieldset className="pick">
            <legend className="sec-title">Роль</legend>
            <div role="radiogroup" aria-label="Роль" className="role-grid">
              {ROLES.map((x) => (
                <button type="button" role="radio" aria-checked={r === x.id} key={x.id} className="role-card" onClick={() => setR(x.id)}>
                  <span className="role-card__name">{x.label}</span>
                  <span className="role-card__hint">{x.hint}</span>
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
