import { useCallback, useEffect, useRef, useState } from 'react';
import { Link2, ListChecks, MessageCircleQuestion, Newspaper, Target, ThumbsDown, ThumbsUp, TrendingUp, TriangleAlert } from 'lucide-react';
import { api } from '../api';
import { href } from '../router';
import { useApp, useAsync } from '../store';
import { CiteContext, ClaimList } from '../components/Claims';
import { AiBlock, BankBlock, RelevanceBadge, Sec, SourcesPanel, TraceBlock, UnknownBlock } from '../components/EventBlocks';
import { fmtShort, plural } from '../components/FeedCard';
import { TopicArt } from '../components/Illustrations';
import { QaPanel, type Seed } from '../components/QaPanel';
import { Button, ErrorState, Pill, Skeleton, StateBlock } from '../components/ui';
import { readEvents } from './readState';

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
          <Skeleton h={220} r={40} /><Skeleton h={140} r={32} /><Skeleton h={180} r={32} />
        </div>
      )}
      {error && <ErrorState message={error} onRetry={reload} />}

      {ins && (
        <CiteContext.Provider value={{ evidence: ins.evidence, onCite, role }}>
          {ins.generated_by === 'cached' && (
            <div className="note note--info" role="status"><span>Новые выводы AI временно недоступны, показан сохранённый результат.</span></div>
          )}

          <header className="event-hero" data-topic={ins.event.topic}>
            <div className="event-hero__text">
              <div className="event-hero__meta">
                <RelevanceBadge rel={ins.relevance} />
                <Pill>{fmtShort(ins.event.date)}</Pill>
                <Pill>{ins.event.topic_label}</Pill>
                <Pill>{ins.event.sources_count} {plural(ins.event.sources_count, 'публикация', 'публикации', 'публикаций')}</Pill>
                {ins.event.is_synthetic && !ins.event.user_uploaded && <Pill tone="warn">демо-данные</Pill>}
                {ins.event.user_uploaded && <Pill tone="info">загружено вами</Pill>}
              </div>
              <h1 className="event-title">{ins.event.title}</h1>
              <p className="event-why">{ins.relevance.explanation}</p>
            </div>
            <div className="event-hero__art" aria-hidden><TopicArt topic={ins.event.topic} /></div>
          </header>
          {ins.event.synthetic_link && (
            <p className="note note--warn" role="note"><Link2 size={16} aria-hidden /><span>{ins.event.synthetic_link}</span></p>
          )}

          <div className="event-grid">
            <article className="event-main">
              {ins.status === 'insufficient_data' ? (
                <section className="sec sec--unknown" aria-labelledby="h-ins">
                  <h2 id="h-ins" className="sec__title">Недостаточно данных для вывода</h2>
                  <p>Что есть: {ins.what_happened.summary}</p>
                  <p>Чего не хватает: связи события с признаками вашей компании и подтверждающей аналитики банка.</p>
                  <ul className="unknown-list">
                    {ins.unknowns.map((u, i) => <li key={i}><p className="small muted">{u.how_to_clarify ?? u.text}</p></li>)}
                  </ul>
                  <Button variant="secondary" onClick={() => ask('Почему это событие не связано с моей компанией?')}>Уточнить</Button>
                </section>
              ) : (
                <>
                  <Sec id="h-what" title="Что случилось" icon={<Newspaper size={22} />}>
                    <ClaimList claims={ins.what_happened.facts} />
                  </Sec>

                  <Sec id="h-why" title="Почему важно вам" icon={<Target size={22} />}>
                    <ClaimList claims={ins.why_it_matters} />
                  </Sec>

                  <Sec id="h-risk" title="Риски" icon={<TriangleAlert size={22} />}>
                      <ClaimList claims={ins.risks} tone="risk" empty="Существенных рисков для вашей компании не выявлено." />
                    </Sec>
                    <Sec id="h-opp" title="Возможности" icon={<TrendingUp size={22} />}>
                      <ClaimList claims={ins.opportunities} tone="opp" empty="Явных возможностей не выявлено." />
                    </Sec>

                  <Sec id="h-next" title="Следующий шаг" icon={<ListChecks size={22} />}>
                    <ClaimList claims={ins.next_actions} className="steps" empty="Конкретных действий не сформировано: связь с профилем слабая." />
                  </Sec>

                  <BankBlock data={ins.bank_position} />
                  {ins.ai_conclusion && <AiBlock data={ins.ai_conclusion} />}
                  {ins.unknowns.length > 0 && <UnknownBlock items={ins.unknowns} onAsk={ask} />}
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
              <Button variant="primary" size="xl" block icon={<MessageCircleQuestion size={20} aria-hidden />} onClick={() => setQaOpen(true)} className="ask-btn">Задать вопрос</Button>
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
