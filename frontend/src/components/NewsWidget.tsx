import { ArrowRight, ChevronRight } from 'lucide-react';
import { href } from '../router';
import { useApp } from '../store';
import { roleLabel } from '../data/companies';
import { useFeed } from './Shell';
import { plural } from './FeedCard';
import { ErrorState, SignalMark, SIG_LABEL, Skeleton } from './ui';

/** Точка входа на главном экране (критерий 09): светлый блок с крупной цифрой, sticky справа. */
export function NewsWidget() {
  const { company, role } = useApp();
  const { items, loading, error, reload } = useFeed();
  const top = (items ?? []).slice(0, 3);
  const high = (items ?? []).filter((i) => i.relevance.level === 'high').length;

  return (
    <section className="news-widget" aria-labelledby="nw-title">
      <p className="overline widget-eyebrow">Новости и аналитика</p>
      <h2 id="nw-title" className="widget-title">
        {loading || error ? 'События для вас' : high > 0
          ? <><span className="widget-num">{high}</span> {plural(high, 'важное событие', 'важных события', 'важных событий')} для вас</>
          : 'Важных событий нет'}
      </h2>
      <p className="small widget-sub">{company?.name} · {role ? roleLabel(role) : ''}</p>

      {loading && (
        <div className="stack gap-16" aria-busy="true">
          <p className="sr-only" role="status">Загружаем события…</p>
          {[0, 1, 2].map((i) => (<div key={i} className="stack gap-8"><Skeleton h={14} w={80} /><Skeleton h={18} w="90%" /></div>))}
        </div>
      )}

      {error && <div className="widget-error"><ErrorState message={error} onRetry={reload} /></div>}

      {!loading && !error && top.length === 0 && <p className="widget-empty">Нет значимых событий за 7 дней.</p>}

      {!loading && !error && top.length > 0 && (
        <ul className="widget-list">
          {top.map((it) => (
            <li key={it.event.event_id}>
              <a className="widget-item" href={href.event(it.event.event_id)}>
                <span className={`widget-item__sig sig sig--${it.relevance.level}`}>
                  <SignalMark level={it.relevance.level} />{SIG_LABEL[it.relevance.level]}
                </span>
                <span className="widget-item__title">{it.event.title}</span>
                <span className="widget-item__why small">{it.why_short.replace(/^Связано:\s*/, '')}</span>
                <ChevronRight className="widget-item__chev" size={20} aria-hidden />
              </a>
            </li>
          ))}
        </ul>
      )}

      <a className="widget-all" href={href.feed}>Все события <ArrowRight size={18} aria-hidden /></a>
    </section>
  );
}
