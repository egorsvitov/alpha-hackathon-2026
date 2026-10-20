import { useState } from 'react';
import { ArrowUpRight, ThumbsDown, ThumbsUp } from 'lucide-react';
import type { FeedItem } from '../types';
import { href } from '../router';
import { SignificanceBadge } from './ui';

export const fmtDate = (iso: string) => {
  const [y, m, d] = iso.slice(0, 10).split('-');
  return `${d}.${m}.${y}`;
};

const MONTHS = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
export const fmtShort = (iso: string) => {
  const [, m, d] = iso.slice(0, 10).split('-');
  return `${Number(d)} ${MONTHS[Number(m) - 1]}`;
};

export function plural(n: number, one: string, few: string, many: string) {
  const m10 = n % 10; const m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20)) return few;
  return many;
}

/** Строка ленты. Без карточки: заголовок, одна строка «почему вам», тонкая линия снизу. */
export function FeedCard({ item, read, lead }: { item: FeedItem; read: boolean; lead?: boolean }) {
  const [vote, setVote] = useState<'up' | 'down' | null>(null);
  const { event, relevance } = item;
  const linked = relevance.factors.some((f) => !f.feature_key.startsWith('meta:'));
  return (
    <article className={`feed-row${lead ? ' feed-row--lead' : ''}${read ? ' is-read' : ''}`}>
      <div className="feed-row__meta">
        <SignificanceBadge level={relevance.level} />
        <span>{fmtShort(event.date)}</span>
        <span>{event.topic_label}</span>
        {event.user_uploaded && <span className="tag-note tag-note--info">загружено вами</span>}
        {event.is_synthetic && !event.user_uploaded && <span className="tag-note">демо-данные</span>}
      </div>
      <h3 className="feed-row__title">
        <a className="feed-row__link" href={href.event(event.event_id)}>{event.title}</a>
        <ArrowUpRight className="feed-row__arrow" size={22} aria-hidden />
      </h3>
      <p className="feed-row__why">
        <span className="feed-row__why-label">Вам:</span> {item.why_short.replace(/^Связано:\s*/, '')}
      </p>
      <footer className="feed-row__foot">
        <span>{event.sources_count} {plural(event.sources_count, 'источник', 'источника', 'источников')}</span>
        {item.has_bank_position && linked && <span className="bank-flag">есть позиция банка</span>}
        {read && <span>просмотрено</span>}
        <span className="feed-row__votes">
          <button type="button" className={`vote${vote === 'up' ? ' is-on' : ''}`} aria-pressed={vote === 'up'} aria-label="Полезно" onClick={() => setVote(vote === 'up' ? null : 'up')}><ThumbsUp size={16} /></button>
          <button type="button" className={`vote${vote === 'down' ? ' is-on' : ''}`} aria-pressed={vote === 'down'} aria-label="Не релевантно" onClick={() => setVote(vote === 'down' ? null : 'down')}><ThumbsDown size={16} /></button>
        </span>
      </footer>
    </article>
  );
}
