import { createContext, useContext } from 'react';
import type { Claim, Evidence, Role } from '../types';
import { featureLabel, roleLabel } from '../data/companies';

/* Связь «утверждение ↔ источник»: клик по [n] подсвечивает строку в панели источников (критерий 12). */

interface CiteCtx {
  evidence: Evidence[];
  onCite(id: string): void;
  role: Role | null;
}
export const CiteContext = createContext<CiteCtx>({ evidence: [], onCite: () => undefined, role: null });

export function Cites({ ids }: { ids: string[] }) {
  const { evidence, onCite } = useContext(CiteContext);
  const nums = ids
    .map((id) => ({ id, n: evidence.filter((e) => e.type !== 'company_feature').findIndex((e) => e.evidence_id === id) + 1 }))
    .filter((x) => x.n > 0)
    .sort((a, b) => a.n - b.n);
  if (!nums.length) return null;
  return (
    <span className="cites">
      {nums.map(({ id, n }) => (
        <button type="button" key={id} className="cite" onClick={() => onCite(id)} aria-label={`Источник ${n}`}>{n}</button>
      ))}
    </span>
  );
}

/** Признаки клиента, повлиявшие на вывод, — одной строкой под утверждением (критерий 12). */
export function Basis({ keys }: { keys?: string[] }) {
  const { evidence } = useContext(CiteContext);
  if (!keys?.length) return null;
  const label = (k: string) => evidence.find((e) => e.evidence_id === `cf:${k}`)?.title ?? featureLabel(k);
  return (
    <span className="claim__basis" aria-label="Признаки клиента, повлиявшие на вывод">
      <span className="claim__basis-label">Основано на:</span> {keys.map(label).join(' · ')}
    </span>
  );
}

export function ClaimItem({ claim, tone }: { claim: Claim; tone?: 'risk' | 'opp' }) {
  const { role } = useContext(CiteContext);
  const forRole = Boolean(role && claim.roles?.includes(role));
  return (
    <li className={`claim${tone ? ` claim--${tone}` : ''}${forRole ? ' claim--for-role' : ''}`}>
      {claim.roles && (
        <span className={`claim__role${forRole ? '' : ' claim__role--other'}`}>
          {forRole && role ? `Для вас, ${roleLabel(role).toLowerCase()}` : `Для роли: ${claim.roles.map((r) => roleLabel(r).toLowerCase()).join(', ')}`}
        </span>
      )}
      <p className="claim__text">
        {claim.conditional && <span className="cond">Условно: </span>}
        {claim.text}<Cites ids={claim.evidence_ids} />
      </p>
      <Basis keys={claim.features} />
    </li>
  );
}

export function ClaimList({ claims, tone, empty, className = '' }: { claims: Claim[]; tone?: 'risk' | 'opp'; empty?: string; className?: string }) {
  if (!claims.length) return <p className="muted">{empty ?? 'Не выявлено.'}</p>;
  return <ul className={`claim-list ${className}`}>{claims.map((c, i) => <ClaimItem key={i} claim={c} tone={tone} />)}</ul>;
}
