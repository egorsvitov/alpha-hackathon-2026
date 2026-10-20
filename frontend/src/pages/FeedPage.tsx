import { useMemo, useState } from 'react';
import { Info, Newspaper, Upload } from 'lucide-react';
import type { SignificanceLevel } from '../types';
import { navigate, href } from '../router';
import { useApp } from '../store';
import { profileChips, roleLabel } from '../data/companies';
import { FeedCard, plural } from '../components/FeedCard';
import { useFeed } from '../components/Shell';
import { UploadModal } from '../components/UploadModal';
import { Button, ErrorState, FeedSkeleton, StateBlock } from '../components/ui';
import { readEvents } from './readState';

type Filter = 'all' | SignificanceLevel;
const FILTERS: { id: Filter; label: string }[] = [
  { id: 'all', label: 'Все' },
  { id: 'high', label: 'Высокая' },
  { id: 'medium', label: 'Средняя' },
  { id: 'low', label: 'Низкая' },
];

export function FeedPage() {
  const { company, role, demo } = useApp();
  const { items, loading, error, reload } = useFeed();
  const [filter, setFilter] = useState<Filter>('all');
  const [upload, setUpload] = useState(false);

  const shown = useMemo(() => (items ?? []).filter((i) => filter === 'all' || i.relevance.level === filter), [items, filter]);
  const counts = useMemo(() => {
    const c = { all: 0, high: 0, medium: 0, low: 0 } as Record<Filter, number>;
    (items ?? []).forEach((i) => { c.all++; c[i.relevance.level]++; });
    return c;
  }, [items]);

  return (
    <div className="feed-page">
      <div className="feed-main">
        <header className="page-head">
          <h1 className="page-title">Новости и аналитика</h1>
          <p className="muted">
            Для {company?.name}, роль «{role ? roleLabel(role) : ''}». Порядок зависит от профиля компании и роли.
          </p>
        </header>

        {demo.budgetExhausted && (
          <div className="note note--info" role="status">
            <Info size={18} aria-hidden />
            <span>Новые выводы AI временно недоступны, показаны сохранённые.</span>
          </div>
        )}

        <div className="tabs" role="group" aria-label="Фильтр по значимости">
          {FILTERS.map((f) => (
            <button type="button" key={f.id} className="tab" aria-pressed={filter === f.id} onClick={() => setFilter(f.id)}>
              {f.label}{items ? <span className="tab__n">{counts[f.id]}</span> : null}
            </button>
          ))}
        </div>

        {loading && <FeedSkeleton />}
        {error && <ErrorState message={error} onRetry={reload} />}
        {!loading && !error && shown.length === 0 && (
          <StateBlock
            icon={<Newspaper size={24} aria-hidden />}
            title={`Значимых событий для ${company?.name ?? 'компании'} за период нет`}
            text="Попробуйте сбросить фильтр или загрузите свою новость."
            action={<Button variant="secondary" onClick={() => setFilter('all')}>Сбросить фильтры</Button>}
          />
        )}
        {!loading && !error && shown.length > 0 && (
          <div className="feed-list">
            {shown.map((it, i) => <FeedCard key={it.event.event_id} item={it} lead={i === 0 && filter === 'all'} read={readEvents.has(it.event.event_id)} />)}
          </div>
        )}
        {!loading && items && <p className="small muted feed-note">{shown.length} {plural(shown.length, 'событие', 'события', 'событий')}. Новости и данные демо — синтетические.</p>}
      </div>

      <aside className="feed-aside">
        <section aria-labelledby="prof-title">
          <h2 id="prof-title" className="section-label">Что мы знаем о вашей компании</h2>
          <dl className="facts">
            {company && profileChips(company).map((c) => {
              const i = c.label.indexOf(': ');
              const term = c.label.slice(0, i); const val = c.label.slice(i + 2);
              return (
                <div key={c.label} className={c.known ? '' : 'is-unknown'}>
                  <dt>{term}</dt>
                  <dd>{val}</dd>
                </div>
              );
            })}
          </dl>
          <p className="small muted">Профиль {company?.profile_version}. «Неизвестно» не означает «нет».</p>
        </section>
        <Button size="l" block icon={<Upload size={18} aria-hidden />} onClick={() => setUpload(true)}>Загрузить новость</Button>
      </aside>

      {upload && <UploadModal onClose={() => setUpload(false)} onDone={(id) => { setUpload(false); navigate(href.event(id)); }} />}
    </div>
  );
}
