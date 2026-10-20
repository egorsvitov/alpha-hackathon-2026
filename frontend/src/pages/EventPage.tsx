import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { Link2, MessageCircleQuestion, ThumbsDown, ThumbsUp, TriangleAlert } from 'lucide-react';
import { api } from '../api';
import { href } from '../router';
import { useApp, useAsync } from '../store';
import { CiteContext, ClaimList } from '../components/Claims';
import { AiBlock, BankBlock, RelevanceBadge, SourcesPanel, TraceBlock, UnknownBlock } from '../components/EventBlocks';
import { fmtShort, plural } from '../components/FeedCard';
import { QaPanel, type Seed } from '../components/QaPanel';
import { Button, ErrorState, Skeleton, StateBlock } from '../components/ui';
import { readEvents } from './readState';

/** Раздел записки: слева метка, справа содержание. На узких экранах метка встаёт над текстом. */
function Row({ id, label, children, tone }: { id: string; label: string; children: ReactNode; tone?: 'bank' | 'ai' | 'unknown' }) {
  return (
    <section className={`rowsec${tone ? ` rowsec--${tone}` : ''}`} aria-labelledby={id}>
      <h2 id={id} className="rowsec__label">{label}</h2>
      <div className="rowsec__body">{children}</div>
    </section>
  );
}

export function EventPage({ eventId }: { eventId: string }) {
  const { company, role } = useApp();
  const { data: ins, loading, error, reload } = useAsync(
    () => api.getInsight(eventId, company!.company_id, role!),
    [eventId, company?.company_id, role],
  );
  const [highlight, setHighlight] = useState<string | null>(null);
  const [qaOpen, setQaOpen] = useState(false);
  const [seed, setSeed] = useState<Seed | null>(null);
  const [vote, setVote] = useState<null | boolean>(null);
  const timer = useRef<number>();

  useEffect(() => { readEvents.add(eventId); setVote(null); setQaOpen(false); setSeed(null); }, [eventId]);

  const onCite = useCallback((id: string) => {
    setHighlight(id);
    document.getElementById(`src-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setHighlight(null), 2200);
  }, []);

  const ask = (q: string) => { setSeed({ q, nonce: Date.now() }); setQaOpen(true); };

  return (
    <div className="event-page">
      <nav className="crumbs small" aria-label="Хлебные крошки">
        <a href={href.feed}>Новости и аналитика</a><span aria-hidden> / </span><span>Событие</span>
      </nav>

      {loading && (
        <div className="stack gap-24" aria-busy="true">
          <p className="sr-only" role="status">Готовим интерпретацию…</p>
          <Skeleton h={14} w={220} r={4} /><Skeleton h={40} w="75%" /><Skeleton h={20} w="45%" />
          <Skeleton h={120} r={12} /><Skeleton h={160} r={12} />
        </div>
      )}
      {error && <ErrorState message={error} onRetry={reload} />}

      {ins && (
        <CiteContext.Provider value={{ evidence: ins.evidence, onCite, role }}>
          <div className="event-grid">
            <article className="event-main">
              {ins.generated_by === 'cached' && (
                <div className="note note--info" role="status"><span>Новые выводы AI временно недоступны, показан сохранённый результат.</span></div>
              )}

              <header className="event-head">
                <div className="event-head__meta">
                  <RelevanceBadge rel={ins.relevance} />
                  <span>{fmtShort(ins.event.date)}</span>
                  <span>{ins.event.topic_label}</span>
                  <span>{ins.event.sources_count} {plural(ins.event.sources_count, 'публикация', 'публикации', 'публикаций')}{ins.event.dedup_note ? `, объединены` : ''}</span>
                  {ins.event.is_synthetic && !ins.event.user_uploaded && <span className="tag-note">демо-данные</span>}
                  {ins.event.user_uploaded && <span className="tag-note tag-note--info">загружено вами</span>}
                </div>
                <h1 className="event-title">{ins.event.title}</h1>
                <p className="lead muted event-why">{ins.relevance.explanation}</p>
                {ins.event.synthetic_link && (
                  <p className="note note--warn" role="note"><Link2 size={16} aria-hidden /><span>{ins.event.synthetic_link}</span></p>
                )}
              </header>

              {ins.status === 'insufficient_data' ? (
                <Row id="h-ins" label="Недостаточно данных" tone="unknown">
                  <p className="lead">Вывод не сформирован.</p>
                  <p>Что есть: {ins.what_happened.summary}</p>
                  <p>Чего не хватает: связи события с признаками вашей компании и подтверждающей аналитики банка.</p>
                  <ul className="unknown-list">
                    {ins.unknowns.map((u, i) => <li key={i}><p className="small muted">{u.how_to_clarify ?? u.text}</p></li>)}
                  </ul>
                  <Button variant="secondary" onClick={() => ask('Почему это событие не связано с моей компанией?')}>Уточнить</Button>
                </Row>
              ) : (
                <>
                  <Row id="h-what" label="Что случилось">
                    <ClaimList claims={ins.what_happened.facts} />
                  </Row>

                  <Row id="h-why" label="Почему важно вам">
                    <ClaimList claims={ins.why_it_matters} />
                  </Row>

                  <Row id="h-risk" label="Риски">
                    <ClaimList claims={ins.risks} tone="risk" empty="Существенных рисков для вашей компании не выявлено." />
                  </Row>

                  <Row id="h-opp" label="Возможности">
                    <ClaimList claims={ins.opportunities} tone="opp" empty="Явных возможностей не выявлено." />
                  </Row>

                  <Row id="h-next" label="Следующий шаг">
                    <ClaimList claims={ins.next_actions} className="steps" empty="Конкретных действий не сформировано: связь с профилем слабая." />
                  </Row>

                  <Row id="h-bank" label="Позиция банка" tone="bank">
                    <BankBlock data={ins.bank_position} />
                  </Row>

                  {ins.ai_conclusion && (
                    <Row id="h-ai" label="Вывод AI" tone="ai">
                      <AiBlock data={ins.ai_conclusion} />
                    </Row>
                  )}

                  {ins.unknowns.length > 0 && (
                    <Row id="h-unk" label="Что неизвестно" tone="unknown">
                      <UnknownBlock items={ins.unknowns} onAsk={ask} />
                    </Row>
                  )}
                </>
              )}

              <TraceBlock insight={ins} />

              <section className="feedback" aria-label="Обратная связь">
                <span className="medium">Карточка была полезной?</span>
                <button type="button" className={`vote vote--l${vote === true ? ' is-on' : ''}`} aria-pressed={vote === true} onClick={() => { setVote(true); void api.sendFeedback(ins.run_id, true); }}><ThumbsUp size={16} aria-hidden /> Да</button>
                <button type="button" className={`vote vote--l${vote === false ? ' is-on' : ''}`} aria-pressed={vote === false} onClick={() => { setVote(false); void api.sendFeedback(ins.run_id, false); }}><ThumbsDown size={16} aria-hidden /> Нет</button>
                {vote !== null && <span className="small muted" role="status">Спасибо, отметка учтена при отборе тем.</span>}
              </section>
            </article>

            <aside className="event-aside">
              <Button variant="primary" size="l" block icon={<MessageCircleQuestion size={20} aria-hidden />} onClick={() => setQaOpen(true)} className="ask-btn">Задать вопрос</Button>
              <SourcesPanel evidence={ins.evidence} highlight={highlight} />
            </aside>
          </div>

          <button type="button" className="fab" aria-label="Задать вопрос" onClick={() => setQaOpen(true)}><MessageCircleQuestion size={24} /></button>
          <QaPanel key={ins.event.event_id + ins.company_id + ins.user_role} insight={ins} open={qaOpen} onClose={() => setQaOpen(false)} seed={seed} />
        </CiteContext.Provider>
      )}

      {!loading && !error && !ins && <StateBlock icon={<TriangleAlert size={24} />} title="Событие не найдено" action={<a className="btn btn--secondary btn--m" href={href.feed}>К ленте</a>} />}
    </div>
  );
}
