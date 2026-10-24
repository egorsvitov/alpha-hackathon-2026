import { useEffect, useMemo, useRef, useState } from 'react';
import { EyeOff, Info, Newspaper, Undo2, Upload } from 'lucide-react';
import type { FeedItem, SignificanceLevel } from '../types';
import { api, DEMO_TODAY } from '../api';
import { navigate, href } from '../router';
import { useApp } from '../store';
import { profileChips, roleLabel } from '../data/companies';
import { FeedCard, fmtShort, plural } from '../components/FeedCard';
import { useFeed } from '../components/Shell';
import { UploadModal } from '../components/UploadModal';
import { Button, ErrorState, FeedSkeleton, SIG_LABEL, SignalMark, StateBlock } from '../components/ui';
import { readEvents } from './readState';

type Filter = 'all' | SignificanceLevel;
type Tab = 'feed' | 'week' | 'history';

const FILTERS: { id: Filter; label: string }[] = [
  { id: 'all', label: 'Все' },
  { id: 'high', label: 'Высокая' },
  { id: 'medium', label: 'Средняя' },
  { id: 'low', label: 'Низкая' },
];

const TABS: { id: Tab; label: string }[] = [
  { id: 'feed', label: 'Лента инсайтов' },
  { id: 'week', label: 'Сводка недели' },
  { id: 'history', label: 'История' },
];

const DAY = 86_400_000;
const ageDays = (iso: string | null) => iso ? Math.round(((api.mode === 'mock' ? Date.parse(DEMO_TODAY) : Date.now()) - Date.parse(iso)) / DAY) : Infinity;

const MONTHS = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь', 'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь'];
const monthKey = (iso: string | null) => iso ? `${MONTHS[Number(iso.slice(5, 7)) - 1]} ${iso.slice(0, 4)}` : 'Дата неизвестна';

export function FeedPage() {
  const { company, role, demo, hidden, hide, unhide } = useApp();
  const { items, processing, loading, error, reload } = useFeed();
  const [tab, setTab] = useState<Tab>('feed');
  const [filter, setFilter] = useState<Filter>('all');
  const [upload, setUpload] = useState(false);
  const [undo, setUndo] = useState<{ id: string; title: string } | null>(null);
  const undoTimer = useRef<number>();

  useEffect(() => () => window.clearTimeout(undoTimer.current), []);

  const onHide = (it: FeedItem) => {
    hide(it.event.event_id);
    setUndo({ id: it.event.event_id, title: it.event.title });
    window.clearTimeout(undoTimer.current);
    undoTimer.current = window.setTimeout(() => setUndo(null), 7000);
  };

  const visible = useMemo(() => (items ?? []).filter((i) => !hidden.includes(i.event.event_id)), [items, hidden]);
  const shown = useMemo(() => visible.filter((i) => filter === 'all' || i.relevance.level === filter), [visible, filter]);
  const counts = useMemo(() => {
    const c = { all: 0, high: 0, medium: 0, low: 0 } as Record<Filter, number>;
    visible.forEach((i) => { c.all++; c[i.relevance.level]++; });
    return c;
  }, [visible]);

  const week = useMemo(() => visible.filter((i) => ageDays(i.event.date) <= 7), [visible]);

  return (
    <div className="feed-page">
      <div className="feed-main">
        <header className="page-head">
          <h1 className="page-title">Новости и аналитика</h1>
          <p className="muted">
            Для {company?.name}, роль «{role ? roleLabel(role) : ''}». Мы отбираем события, значимые для вашего бизнеса, и объясняем почему.
          </p>
        </header>

        {demo.budgetExhausted && (
          <div className="note note--info" role="status">
            <Info size={18} aria-hidden />
            <span>Новые выводы AI временно недоступны, показаны сохранённые.</span>
          </div>
        )}

        <div className="segmented segmented--wide tabs" role="group" aria-label="Раздел">
          {TABS.map((t) => (
            <button type="button" key={t.id} className="segmented__item" aria-pressed={tab === t.id} onClick={() => setTab(t.id)}>
              {t.label}
              {t.id === 'history' && hidden.length > 0 && <span className="segmented__n">{hidden.length} скрыто</span>}
            </button>
          ))}
        </div>

        {processing && processing.status !== 'demo' && (
          <div className="note note--info" role="status"><span>
            {processing.message} {Object.entries(processing.counts ?? {}).map(([status, count]) => `${status}: ${count}`).join(' · ')}
            {processing.error_code && ` Код: ${processing.error_code}`}
            {processing.ingestion?.status === 'failed' && ` Обновление индекса не удалось: ${processing.ingestion.code}.`}
          </span></div>
        )}
        {loading && <FeedSkeleton />}
        {error && <ErrorState message={error} onRetry={reload} />}

        {!loading && !error && tab === 'feed' && (
          <>
            <div className="segmented segmented--wide" role="group" aria-label="Фильтр по значимости">
              {FILTERS.map((f) => (
                <button type="button" key={f.id} className="segmented__item" aria-pressed={filter === f.id} onClick={() => setFilter(f.id)}>
                  {f.label}<span className="segmented__n">{counts[f.id]}</span>
                </button>
              ))}
            </div>
            {shown.length === 0 ? (
              <StateBlock
                icon={<Newspaper size={24} aria-hidden />}
                title={processing && !['ready', 'demo'].includes(processing.status) ? 'Обработка новостей не завершена' : `Значимых событий для ${company?.name ?? 'компании'} за период нет`}
                text={processing && !['ready', 'demo'].includes(processing.status) ? processing.message ?? 'Проверьте статус задания.' : 'Попробуйте сбросить фильтр или посмотрите историю.'}
                action={<Button variant="secondary" onClick={() => setFilter('all')}>Сбросить фильтры</Button>}
              />
            ) : (
              <div className="feed-grid">
                {shown.map((it, i) => (
                  <FeedCard key={it.event.event_id} item={it} lead={i === 0 && filter === 'all'} read={readEvents.has(it.event.event_id)} onHide={() => onHide(it)} />
                ))}
              </div>
            )}
            <p className="small muted feed-note">{shown.length} {plural(shown.length, 'событие', 'события', 'событий')}. {api.mode === 'mock' ? 'Новости и данные демо — синтетические.' : 'Сохранённые результаты анализа. Полнота и источники указаны в карточке.'}</p>
          </>
        )}

        {!loading && !error && tab === 'week' && <WeekDigest items={week} companyName={company?.name ?? ''} />}

        {!loading && !error && tab === 'history' && (
          <History items={items ?? []} hidden={hidden} onUnhide={unhide} />
        )}

        {undo && (
          <div className="toast" role="status">
            <span>Карточка скрыта: «{undo.title}». Она осталась в истории.</span>
            <button type="button" className="toast__btn" onClick={() => { unhide(undo.id); setUndo(null); }}><Undo2 size={16} aria-hidden /> Вернуть</button>
          </div>
        )}
      </div>

      <aside className="feed-aside">
        <section className="profile-card" aria-labelledby="prof-title">
          <h2 id="prof-title" className="sec-title">Что мы знаем о вашей компании</h2>
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

/* ---------------- Сводка недели ---------------- */

function WeekDigest({ items, companyName }: { items: FeedItem[]; companyName: string }) {
  const high = items.filter((i) => i.relevance.level === 'high');
  const top = items.slice(0, 5);
  const topics = useMemo(() => {
    const m = new Map<string, number>();
    items.forEach((i) => m.set(i.event.topic_label, (m.get(i.event.topic_label) ?? 0) + 1));
    return [...m.entries()].sort((a, b) => b[1] - a[1]);
  }, [items]);

  if (items.length === 0) {
    return (
      <StateBlock
        icon={<Newspaper size={24} aria-hidden />}
        title="За неделю значимых событий нет"
        text="Посмотрите раздел «История» или вернитесь позже: сводка собирается автоматически."
      />
    );
  }
  return (
    <section className="digest" aria-labelledby="digest-title">
      <h2 id="digest-title" className="sec-title">Итоги недели для {companyName}</h2>
      <p className="digest__lead">
        За 7 дней вышло {items.length} {plural(items.length, 'событие', 'события', 'событий')}, из них {high.length} с высокой значимостью.
        {high.length > 0 ? ' Начните с самых важных.' : ' Срочных решений не требуется.'}
      </p>
      {topics.length > 0 && (
        <ul className="digest__topics small" aria-label="Темы недели">
          {topics.map(([t, n]) => <li key={t}>{t} · {n}</li>)}
        </ul>
      )}
      <ol className="digest__list">
        {top.map((it) => (
          <li key={it.event.event_id}>
            <a className="digest__item" href={href.event(it.event.event_id)}>
              <span className={`sig sig--${it.relevance.level}`}><SignalMark level={it.relevance.level} />{SIG_LABEL[it.relevance.level]}</span>
              <span className="digest__title">{it.event.title}</span>
              <span className="small muted">{fmtShort(it.event.date)} · {it.why_short.replace(/^Связано:\s*/, '')}</span>
            </a>
          </li>
        ))}
      </ol>
      <p className="small muted">Сводка собирается из ленты инсайтов за последние 7 дней и пересчитывается по профилю компании и роли.</p>
    </section>
  );
}

/* ---------------- История ---------------- */

function History({ items, hidden, onUnhide }: { items: FeedItem[]; hidden: string[]; onUnhide(id: string): void }) {
  const groups = useMemo(() => {
    const sorted = [...items].sort((a, b) => (b.event.date ?? '').localeCompare(a.event.date ?? ''));
    const m = new Map<string, FeedItem[]>();
    sorted.forEach((i) => {
      const k = monthKey(i.event.date);
      m.set(k, [...(m.get(k) ?? []), i]);
    });
    return [...m.entries()];
  }, [items]);

  if (groups.length === 0) {
    return <StateBlock icon={<Newspaper size={24} aria-hidden />} title="История пока пуста" text="События появятся здесь, как только они будут обработаны." />;
  }
  return (
    <div className="history">
      {groups.map(([month, list]) => (
        <section key={month} aria-label={month}>
          <h2 className="sec-title history__month">{month}</h2>
          <ul className="history__list">
            {list.map((it) => {
              const isHidden = hidden.includes(it.event.event_id);
              return (
                <li key={it.event.event_id} className={`history__row${isHidden ? ' is-hidden' : ''}`}>
                  <span className="history__date small muted">{fmtShort(it.event.date)}</span>
                  <a className="history__title" href={href.event(it.event.event_id)}>{it.event.title}</a>
                  <span className={`sig sig--${it.relevance.level}`}><SignalMark level={it.relevance.level} />{SIG_LABEL[it.relevance.level]}</span>
                  {isHidden && (
                    <button type="button" className="link-btn" onClick={() => onUnhide(it.event.event_id)}><EyeOff size={14} aria-hidden /> Скрыта · вернуть</button>
                  )}
                </li>
              );
            })}
          </ul>
        </section>
      ))}
    </div>
  );
}
