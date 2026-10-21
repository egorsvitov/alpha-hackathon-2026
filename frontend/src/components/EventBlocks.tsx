import { useState, type ReactNode } from 'react';
import { CalendarClock, CheckCircle2, ChevronDown, HelpCircle, Landmark, MessageSquareText, PhoneCall, Scale, Sparkles } from 'lucide-react';
import type { ActionKind, Evidence, Insight, TraceStep } from '../types';
import { fmtDate } from './FeedCard';
import { Basis, Cites } from './Claims';
import { ArtSpark } from './Illustrations';
import { Button, IconTile, Modal, SIG_CRITERIA, SIG_LABEL, SignificanceBadge, usePopover } from './ui';

/* ---------------- Раздел-карточка с плиткой-иконкой ---------------- */

export function Sec({ id, title, icon, children, className = '' }: { id: string; title: string; icon: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`sec ${className}`} aria-labelledby={id}>
      <header className="sec__head">
        <h2 id={id} className="sec__title">{title}</h2>
        <IconTile tone="neutral">{icon}</IconTile>
      </header>
      <div className="sec__body">{children}</div>
    </section>
  );
}

/* ---------------- Позиция банка: красная фирменная карточка ---------------- */

export function BankBlock({ data }: { data: Insight['bank_position'] }) {
  if (!data) {
    return (
      <section className="sec sec--bank-empty" aria-labelledby="h-bank">
        <header className="sec__head">
          <h2 id="h-bank" className="sec__title">Позиция банка</h2>
          <IconTile tone="neutral"><Landmark size={22} /></IconTile>
        </header>
        <p className="muted">Позиции банка по этому событию в доступных материалах нет. Мы не формируем её за банк: выводы ниже принадлежат модели.</p>
      </section>
    );
  }
  return (
    <section className="sec sec--bank" aria-labelledby="h-bank">
      <div className="sec--bank__art" aria-hidden><ArtSpark /></div>
      <p className="bank-label"><Landmark size={16} aria-hidden /> Позиция Альфа-Банка</p>
      <h2 id="h-bank" className="sr-only">Позиция банка</h2>
      <blockquote className="bank-quote">
        {data.claims.map((c, i) => (
          <p key={i} className={i === 0 ? 'bank-quote__lead' : ''}>{c.text}<Cites ids={c.evidence_ids} /></p>
        ))}
      </blockquote>
      <p className="bank-meta">
        Аналитика Альфа-Банка{data.as_of ? ` · по состоянию на ${fmtDate(data.as_of)}` : ''} · в демо тексты — иллюстрация, не материалы банка
      </p>
    </section>
  );
}

/* ---------------- Вывод AI: светлая карточка с фиолетовой рамкой и пометкой ---------------- */

export function AiBlock({ data }: { data: NonNullable<Insight['ai_conclusion']> }) {
  return (
    <section className="sec sec--ai" aria-labelledby="h-ai">
      <header className="sec__head">
        <h2 id="h-ai" className="sec__title">Вывод AI</h2>
        <span className="ai-flag"><Sparkles size={14} aria-hidden /> не позиция банка</span>
      </header>
      <p className="sec__text">{data.text}<Cites ids={data.evidence_ids} /></p>
      <Basis keys={data.features} />
      <p className="small muted">Сгенерировано моделью на основе указанных источников и может содержать ошибки. Это не официальная позиция Альфа-Банка и не инвестиционная рекомендация.</p>
    </section>
  );
}

/* ---------------- Оценка эффекта: сценарий с допущениями ---------------- */

export function ImpactBlock({ data }: { data: NonNullable<Insight['impact_estimate']> }) {
  return (
    <section className="sec sec--impact" aria-labelledby="h-impact">
      <header className="sec__head">
        <h2 id="h-impact" className="sec__title">Оценка эффекта</h2>
        <span className="ai-flag"><Scale size={14} aria-hidden /> сценарий, не прогноз</span>
      </header>
      <p className="impact__metric small muted">{data.metric}</p>
      <p className="sec__text">{data.summary}<Cites ids={data.evidence_ids} /></p>
      <div>
        <p className="small medium">Допущения</p>
        <ul className="impact__list small">
          {data.assumptions.map((a) => <li key={a}>{a}</li>)}
        </ul>
      </div>
    </section>
  );
}

/* ---------------- Действия: связаться с банком (имитация интеграции с CRM) ---------------- */

const ACTIONS: { kind: ActionKind; label: string; title: string; icon: ReactNode; done: string }[] = [
  { kind: 'manager', label: 'Написать менеджеру', title: 'Сообщение персональному менеджеру', icon: <MessageSquareText size={18} aria-hidden />, done: 'Сообщение передано менеджеру' },
  { kind: 'request', label: 'Оставить заявку', title: 'Заявка на консультацию', icon: <CheckCircle2 size={18} aria-hidden />, done: 'Заявка принята' },
  { kind: 'callback', label: 'Заказать звонок', title: 'Обратный звонок', icon: <PhoneCall size={18} aria-hidden />, done: 'Звонок заказан' },
];

export function ActionsBlock({ insight }: { insight: Insight }) {
  const [active, setActive] = useState<ActionKind | null>(null);
  const [sent, setSent] = useState<Partial<Record<ActionKind, string>>>({});
  const def = ACTIONS.find((a) => a.kind === active);
  return (
    <section className="sec sec--actions" aria-labelledby="h-act">
      <header className="sec__head">
        <h2 id="h-act" className="sec__title">Обсудить с банком</h2>
        <IconTile tone="neutral"><CalendarClock size={22} /></IconTile>
      </header>
      <p className="muted">Менеджер увидит, по какому событию вы обращаетесь, и подготовится. Мы не навязываем продукты: разговор о том, что важно вашему бизнесу.</p>
      <div className="row gap-8 wrap">
        {ACTIONS.map((a) => (
          <Button key={a.kind} size="l" variant={sent[a.kind] ? 'ghost' : 'secondary'} icon={sent[a.kind] ? <CheckCircle2 size={18} aria-hidden /> : a.icon} onClick={() => setActive(a.kind)}>
            {sent[a.kind] ? a.done : a.label}
          </Button>
        ))}
      </div>
      {Object.values(sent).length > 0 && (
        <p className="small muted" role="status">В демо обращение сохраняется только на этой странице. В банке оно уходит менеджеру через уже подключённые каналы.</p>
      )}
      {def && (
        <ActionModal
          title={def.title}
          kind={def.kind}
          insight={insight}
          onClose={() => setActive(null)}
          onSent={(id) => { setSent((s) => ({ ...s, [def.kind]: id })); setActive(null); }}
        />
      )}
    </section>
  );
}

function ActionModal({ title, kind, insight, onClose, onSent }: { title: string; kind: ActionKind; insight: Insight; onClose(): void; onSent(id: string): void }) {
  const [text, setText] = useState(`Хочу обсудить событие «${insight.event.title}».`);
  const [phone, setPhone] = useState('');
  const [when, setWhen] = useState('today');
  const [busy, setBusy] = useState(false);
  const needPhone = kind === 'callback';
  const submit = () => {
    setBusy(true);
    window.setTimeout(() => onSent(`REQ-${Math.floor(1000 + Math.random() * 9000)}`), 600);
  };
  return (
    <Modal
      title={title}
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" size="l" onClick={onClose}>Отмена</Button>
          <Button variant="primary" size="l" loading={busy} disabled={needPhone ? phone.replace(/\D/g, '').length < 10 : text.trim().length < 3} onClick={submit}>Отправить</Button>
        </>
      }
    >
      <p className="muted small">Событие: {insight.event.title}</p>
      {needPhone && (
        <>
          <div className="field">
            <label htmlFor="act-phone">Телефон</label>
            <input id="act-phone" type="tel" autoComplete="tel" placeholder="+7 900 000-00-00" value={phone} onChange={(e) => setPhone(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="act-when">Когда удобно</label>
            <select id="act-when" value={when} onChange={(e) => setWhen(e.target.value)}>
              <option value="today">Сегодня</option>
              <option value="tomorrow">Завтра</option>
              <option value="week">В течение недели</option>
            </select>
          </div>
        </>
      )}
      <div className="field">
        <label htmlFor="act-text">{needPhone ? 'Комментарий для менеджера' : 'Сообщение'}</label>
        <textarea id="act-text" rows={4} value={text} onChange={(e) => setText(e.target.value)} />
      </div>
    </Modal>
  );
}

/* ---------------- Дисклеймер ---------------- */

export function Disclaimer() {
  return (
    <footer className="disclaimer small muted">
      Материалы носят информационный характер и не являются инвестиционной рекомендацией или офертой. Выводы AI подготовлены моделью и не выражают официальную позицию Альфа-Банка. В демо все данные синтетические.
    </footer>
  );
}

/* ---------------- Что неизвестно: пунктирная карточка ---------------- */

export function UnknownBlock({ items, onAsk }: { items: Insight['unknowns']; onAsk(q: string): void }) {
  return (
    <section className="sec sec--unknown" aria-labelledby="h-unk">
      <header className="sec__head">
        <h2 id="h-unk" className="sec__title">Что неизвестно</h2>
        <IconTile tone="neutral"><HelpCircle size={22} /></IconTile>
      </header>
      <ul className="unknown-list">
        {items.map((u, i) => (
          <li key={i}>
            <p>{u.text}</p>
            {u.how_to_clarify && <p className="small muted">Как уточнить: {u.how_to_clarify}</p>}
            {u.question && <button type="button" className="ask-chip" onClick={() => onAsk(u.question!)}>Спросить об этом</button>}
          </li>
        ))}
      </ul>
    </section>
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
      <h2 id="h-src" className="sec-title">Источники · {docs.length}</h2>
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
          <h3 className="sec-title sources__sub">Признаки клиента</h3>
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
          <p className="small"><span className="medium">{SIG_LABEL[rel.level]} значимость ({SIG_CRITERIA[rel.level].range}).</span> {SIG_CRITERIA[rel.level].text}</p>
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
