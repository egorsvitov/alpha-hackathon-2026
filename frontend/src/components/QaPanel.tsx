import { useEffect, useRef, useState } from 'react';
import { AlertCircle, RefreshCw, Search, Send, Sparkles, X } from 'lucide-react';
import { api } from '../api';
import type { Insight, QaAnswer } from '../types';
import { fmtDate } from './FeedCard';
import { Badge, IconButton } from './ui';

interface Msg {
  id: number;
  from: 'user' | 'ai';
  text: string;
  answer?: QaAnswer;
  error?: boolean;
  question?: string;
}

export interface Seed { q: string; nonce: number }

/** Панель уточняющего вопроса (критерий 10). Ответ строится по контексту карточки; при нехватке данных — дополнительный поиск. */
export function QaPanel({ insight, open, onClose, seed }: { insight: Insight; open: boolean; onClose(): void; seed: Seed | null }) {
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [busy, setBusy] = useState(false);
  const [text, setText] = useState('');
  const listRef = useRef<HTMLDivElement>(null);
  const idRef = useRef(1);
  const lastSeed = useRef<number | null>(null);

  const send = async (q: string) => {
    const question = q.trim();
    if (!question || busy) return;
    setText('');
    setBusy(true);
    const uid = idRef.current++;
    setMsgs((m) => [...m, { id: uid, from: 'user', text: question }]);
    try {
      const answer = await api.ask(insight, question);
      setMsgs((m) => [...m, { id: idRef.current++, from: 'ai', text: answer.text, answer }]);
    } catch (e) {
      setMsgs((m) => [...m, { id: idRef.current++, from: 'ai', text: e instanceof Error ? e.message : 'Ошибка', error: true, question }]);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    if (seed && seed.nonce !== lastSeed.current) {
      lastSeed.current = seed.nonce;
      void send(seed.q);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seed]);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: 'smooth' });
  }, [msgs, busy]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  const suggestions = [
    ...insight.unknowns.map((u) => u.question).filter((q): q is string => Boolean(q)),
    'Что сделать в первую очередь?',
    'Какая позиция банка?',
    'Как это связано с курсом валюты?',
    'Кто мои крупные контрагенты?',
  ].slice(0, 4);

  return (
    <>
      {open && <div className="qa-scrim" onClick={onClose} aria-hidden />}
      <aside className={`qa-panel${open ? ' is-open' : ''}`} aria-hidden={!open} aria-label="Уточняющий вопрос">
        <header className="qa-panel__head">
          <div>
            <h2 className="h4">Вопрос по событию</h2>
            <p className="small muted qa-context" title={insight.event.title}>{insight.event.title}</p>
          </div>
          <IconButton label="Закрыть" onClick={onClose}><X size={20} /></IconButton>
        </header>

        <div className="qa-panel__list" ref={listRef} aria-live="polite">
          {msgs.length === 0 && (
            <div className="qa-hints">
              <p className="muted small">Ответ строится по этой карточке: профилю компании, источникам и аналитике банка. Если данных не хватает, я выполню дополнительный поиск или скажу, что вывод сделать нельзя.</p>
              <div className="qa-chips">
                {suggestions.map((s) => (
                  <button type="button" key={s} className="suggest" onClick={() => void send(s)}>{s}</button>
                ))}
              </div>
            </div>
          )}

          {msgs.map((m) =>
            m.from === 'user' ? (
              <div key={m.id} className="msg msg--user">{m.text}</div>
            ) : (
              <div key={m.id} className={`msg msg--ai${m.error ? ' is-error' : ''}`}>
                {m.error ? (
                  <div className="row gap-8"><AlertCircle size={18} aria-hidden /><span>{m.text}</span></div>
                ) : (
                  <>
                    <div className="row gap-8 msg__tags">
                      <Badge tone="ai" icon={<Sparkles size={14} aria-hidden />}>Ответ AI</Badge>
                      {m.answer?.from_cache && <Badge tone="warning">Сохранённый ответ</Badge>}
                    </div>
                    {m.answer?.did_extra_search && (
                      <p className="small muted row gap-8 extra-search"><Search size={14} aria-hidden /> {m.answer.extra_search_note ?? 'Выполнен дополнительный поиск'}</p>
                    )}
                    <p>{m.text}</p>
                    {m.answer && m.answer.evidence.length > 0 && (
                      <div className="msg__sources">
                        <p className="overline">Источники ответа</p>
                        <ul>
                          {m.answer.evidence.map((e) => (
                            <li key={e.evidence_id} className="small">
                              <span className="medium">{e.title}</span>
                              <span className="muted"> · {e.publisher}{e.date ? ` · ${fmtDate(e.date)}` : ''}</span>
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}
                    {m.answer?.insufficient && <p className="small muted">Данных для уверенного вывода недостаточно.</p>}
                  </>
                )}
                {m.error && m.question && (
                  <button type="button" className="link-btn" onClick={() => void send(m.question!)}><RefreshCw size={14} aria-hidden /> Повторить</button>
                )}
              </div>
            ),
          )}

          {busy && (
            <div className="msg msg--ai msg--typing" role="status">
              <span className="dots" aria-hidden><i /><i /><i /></span> Ищем в источниках…
            </div>
          )}
        </div>

        <form className="qa-panel__form" onSubmit={(e) => { e.preventDefault(); void send(text); }}>
          <textarea
            value={text}
            rows={1}
            placeholder="Задайте уточняющий вопрос"
            aria-label="Уточняющий вопрос"
            onChange={(e) => { setText(e.target.value); e.target.style.height = 'auto'; e.target.style.height = `${Math.min(e.target.scrollHeight, 120)}px`; }}
            onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void send(text); } }}
          />
          <button type="submit" className="send-btn" aria-label="Отправить" disabled={busy || !text.trim()}><Send size={18} /></button>
        </form>
      </aside>
    </>
  );
}
