import { useState } from 'react';
import { EyeOff, Landmark, ThumbsDown, ThumbsUp } from 'lucide-react';
import type { FeedItem } from '../types';
import { href } from '../router';
import { TopicArt } from './Illustrations';
import { Pill, SignificanceBadge } from './ui';

export const fmtDate = (iso: string | null) => {
  if (!iso) return 'Дата неизвестна';
  const [y, m, d] = iso.slice(0, 10).split('-');
  return `${d}.${m}.${y}`;
};

const MONTHS = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
export const fmtShort = (iso: string | null) => {
  if (!iso) return 'Дата неизвестна';
  const [, m, d] = iso.slice(0, 10).split('-');
  return `${Number(d)} ${MONTHS[Number(m) - 1]}`;
};

export function plural(n: number, one: string, few: string, many: string) {
  const m10 = n % 10; const m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20)) return few;
  return many;
}

/** Карточка ленты в стиле бенто: цвет и иллюстрация задаются темой события. */
export function FeedCard({ item, read, lead, onHide }: { item: FeedItem; read: boolean; lead?: boolean; onHide?: () => void }) {
  const [vote, setVote] = useState<'up' | 'down' | null>(null);
  const { event, relevance } = item;
  const linked = relevance.factors.some((f) => !f.feature_key.startsWith('meta:')) || (relevance.reasons?.length ?? 0) > 0;
  return (
    <article className={`topic-card${lead ? ' topic-card--lead' : ''}${read ? ' is-read' : ''}`} data-topic={event.topic}>
      <div className="topic-card__top">
        <SignificanceBadge level={relevance.level} />
        <Pill>{fmtShort(event.date)} · {event.topic_label}</Pill>
      </div>
      <h3 className="topic-card__title">
        <a className="topic-card__link" href={href.event(event.event_id)}>{event.title}</a>
      </h3>
      <p className="topic-card__why">
        <span className="topic-card__why-label">Вам:</span> {item.why_short.replace(/^Связано:\s*/, '')}
      </p>
      <footer className="topic-card__foot">
        <Pill>{event.sources_count} {plural(event.sources_count, 'публикация', 'публикации', 'публикаций')}</Pill>
        {item.has_bank_position && linked && <Pill tone="dark"><Landmark size={13} aria-hidden /> позиция банка</Pill>}
        {read && <Pill>просмотрено</Pill>}
        {event.processing_status === 'merge_fallback' && <Pill tone="warn">Без merge · возможны повторы</Pill>}
        {event.user_uploaded && <Pill tone="info">загружено вами</Pill>}
        {event.is_synthetic && !event.user_uploaded && <Pill tone="warn">демо-данные</Pill>}
        <span className="topic-card__votes">
          <button type="button" className={`vote${vote === 'up' ? ' is-on' : ''}`} aria-pressed={vote === 'up'} aria-label="Полезно" onClick={() => setVote(vote === 'up' ? null : 'up')}><ThumbsUp size={16} /></button>
          <button type="button" className={`vote${vote === 'down' ? ' is-on' : ''}`} aria-pressed={vote === 'down'} aria-label="Не релевантно" onClick={() => setVote(vote === 'down' ? null : 'down')}><ThumbsDown size={16} /></button>
          {onHide && <button type="button" className="vote" aria-label="Скрыть карточку" title="Скрыть" onClick={onHide}><EyeOff size={16} /></button>}
        </span>
      </footer>
      <div className="topic-card__art" aria-hidden><TopicArt topic={event.topic} /></div>
    </article>
  );
}
