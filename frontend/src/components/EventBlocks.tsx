import { useState } from 'react';
import { ChevronDown, Sparkles } from 'lucide-react';
import type { Evidence, Insight, TraceStep } from '../types';
import { fmtDate } from './FeedCard';
import { Basis, Cites } from './Claims';
import { SignificanceBadge, usePopover } from './ui';

/* Содержимое блоков; заголовки-метки ставит страница (EventPage → Row). */

/* ---------------- Позиция банка: цитата с засечками и красной линией ---------------- */

export function BankBlock({ data }: { data: Insight['bank_position'] }) {
  if (!data) {
    return <p className="muted">Позиции банка по этому событию в доступных материалах нет. Мы не формируем её за банк — выводы ниже принадлежат модели.</p>;
  }
  return (
    <figure className="bank-quote">
      <blockquote>
        {data.claims.map((c, i) => (
          <p key={i} className={i === 0 ? 'bank-quote__lead' : ''}>{c.text}<Cites ids={c.evidence_ids} /></p>
        ))}
      </blockquote>
      <figcaption>
        Аналитика Альфа-Банка{data.as_of ? ` · по состоянию на ${fmtDate(data.as_of)}` : ''}
        <span className="bank-quote__demo"> · в демо тексты — иллюстрация, не материалы банка</span>
      </figcaption>
    </figure>
  );
}

/* ---------------- Вывод AI: обычный текст с фиолетовой линией и явной пометкой ---------------- */

export function AiBlock({ data }: { data: NonNullable<Insight['ai_conclusion']> }) {
  return (
    <div className="ai-note">
      <p className="ai-note__label"><Sparkles size={14} aria-hidden /> Вывод AI — не позиция банка</p>
      <p>{data.text}<Cites ids={data.evidence_ids} /></p>
      <Basis keys={data.features} />
      <p className="small muted ai-note__disclaimer">Сгенерировано моделью на основе указанных источников и может содержать ошибки.</p>
    </div>
  );
}

/* ---------------- Что неизвестно ---------------- */

export function UnknownBlock({ items, onAsk }: { items: Insight['unknowns']; onAsk(q: string): void }) {
  return (
    <ul className="unknown-list">
      {items.map((u, i) => (
        <li key={i}>
          <p>{u.text}</p>
          {u.how_to_clarify && <p className="small muted">Как уточнить: {u.how_to_clarify}</p>}
          {u.question && <button type="button" className="link-btn" onClick={() => onAsk(u.question!)}>Спросить об этом</button>}
        </li>
      ))}
    </ul>
  );
}

/* ---------------- Источники ---------------- */

const TYPE_LABEL: Record<Evidence['type'], string> = {
  news: 'СМИ',
  bank_research: 'Аналитика Альфа-Банка',
  company_feature: 'Данные компании',
  synthetic: 'Синтетические данные',
};

export function SourcesPanel({ evidence, highlight }: { evidence: Evidence[]; highlight: string | null }) {
  const docs = evidence.filter((e) => e.type !== 'company_feature');
  const feats = evidence.filter((e) => e.type === 'company_feature');
  return (
    <section className="sources" aria-labelledby="h-src">
      <h2 id="h-src" className="section-label">Источники · {docs.length}</h2>
      <ol className="source-list">
        {docs.map((e, i) => (
          <li key={e.evidence_id} id={`src-${e.evidence_id}`} className={`source${highlight === e.evidence_id ? ' is-highlight' : ''}`}>
            <span className="source__num" aria-hidden>{i + 1}</span>
            <div className="source__body">
              <p className="source__title">{e.title}</p>
              <p className="source__meta small">
                <span className={`src-type src-type--${e.type}`}>{TYPE_LABEL[e.type]}{e.type === 'bank_research' && e.is_synthetic ? ', демо-цитата' : ''}</span>
                {' · '}{e.publisher}{e.date ? ` · ${fmtDate(e.date)}` : ''}{e.doc ? ` · ${e.doc}${e.page ? `, стр. ${e.page}` : ''}` : ''}
              </p>
              <blockquote className="source__quote">{e.quote}</blockquote>
            </div>
          </li>
        ))}
      </ol>
      {feats.length > 0 && (
        <>
          <h3 className="section-label sources__sub">Признаки клиента</h3>
          <ul className="feature-sources">
            {feats.map((e) => (
              <li key={e.evidence_id} className="small"><span className="medium">{e.title}</span><code>{e.quote}</code></li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

/* ---------------- Значимость и вклад факторов ---------------- */

export function RelevanceBadge({ rel }: { rel: Insight['relevance'] }) {
  const { open, setOpen, ref } = usePopover();
  const max = Math.max(...rel.factors.map((f) => f.contribution), 0.01);
  return (
    <div className="relevance" ref={ref}>
      <button type="button" className="relevance__btn" aria-expanded={open} onClick={() => setOpen(!open)}>
        <SignificanceBadge level={rel.level} />
        <span className="relevance__link">Почему такая значимость <ChevronDown size={14} aria-hidden /></span>
      </button>
      {open && (
        <div className="popover popover--left relevance__pop" role="dialog" aria-label="Вклад факторов в значимость">
          <p className="popover__title">{rel.factors.length ? 'Вклад факторов' : 'Причины'} · итого {rel.score.toFixed(2).replace('.', ',')}</p>
          {rel.factors.length === 0 && (
            <ul className="factor-list">
              {(rel.reasons ?? [rel.explanation]).map((r) => <li key={r} className="small">{r}</li>)}
            </ul>
          )}
          <ul className="factor-list">
            {rel.factors.filter((f) => f.contribution > 0).map((f) => (
              <li key={f.feature_key}>
                <div className="row between small"><span className="medium">{f.label}</span><span className="num">+{f.contribution.toFixed(2).replace('.', ',')}</span></div>
                <div className="bar" aria-hidden><span style={{ width: `${(f.contribution / max) * 100}%` }} /></div>
                <span className="small muted">{f.detail}</span>
              </li>
            ))}
          </ul>
          <p className="small muted">Оценка показывает связь события с профилем и не является оценкой финансового эффекта.</p>
        </div>
      )}
    </div>
  );
}

/* ---------------- Трасса агента ---------------- */

const MARK: Record<TraceStep['status'], string> = { ok: '✓', retry: '↻', warn: '!' };

export function TraceBlock({ insight }: { insight: Insight }) {
  const [open, setOpen] = useState(false);
  const retries = insight.trace.filter((s) => s.status === 'retry').length;
  return (
    <section className="trace" aria-labelledby="h-trace">
      <h2 id="h-trace" className="trace__h">
        <button type="button" className="trace__toggle" aria-expanded={open} onClick={() => setOpen(!open)}>
          Как получен вывод <ChevronDown size={16} aria-hidden className={open ? 'rot' : ''} />
        </button>
      </h2>
      <p className="small muted">
        Запуск {insight.run_id} · профиль {insight.profile_version} · шагов: {insight.trace.length}{retries ? ` · повторных поисков: ${retries}` : ''}
        {insight.generated_by === 'cached' && ' · сохранённый ответ'}
      </p>
      {open && (
        <ol className="trace-list">
          {insight.trace.map((s) => (
            <li key={s.n} className={`trace-step trace-step--${s.status}`}>
              <span className="trace-step__ic" aria-hidden>{MARK[s.status]}</span>
              <div>
                <p className="small"><code>{s.tool}</code> — {s.summary}</p>
                <p className="small muted">{s.result}</p>
              </div>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
