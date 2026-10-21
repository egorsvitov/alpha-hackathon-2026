import { useEffect, useRef, useState, type ButtonHTMLAttributes, type ReactNode } from 'react';
import { AlertCircle, Loader2, X } from 'lucide-react';
import type { SignificanceLevel } from '../types';

/* ---------------- Кнопки ---------------- */

type Variant = 'primary' | 'secondary' | 'ghost' | 'text';
type Size = 's' | 'm' | 'l' | 'xl';

interface BtnProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  block?: boolean;
  loading?: boolean;
  icon?: ReactNode;
}

export function Button({ variant = 'secondary', size = 'm', block, loading, icon, children, className = '', disabled, ...rest }: BtnProps) {
  return (
    <button
      type="button"
      {...rest}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={`btn btn--${variant} btn--${size}${block ? ' btn--block' : ''}${className ? ` ${className}` : ''}`}
    >
      {loading ? <Loader2 className="spin" size={18} aria-hidden /> : icon}
      {children}
    </button>
  );
}

export function IconButton({ label, children, className = '', ...rest }: ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return (
    <button type="button" aria-label={label} title={label} className={`icon-btn ${className}`} {...rest}>
      {children}
    </button>
  );
}

/* ---------------- Значимость ---------------- */

export const SIG_LABEL: Record<SignificanceLevel, string> = { high: 'Высокая', medium: 'Средняя', low: 'Низкая' };

/** Что означает каждый уровень. Пороги заданы в mock-движке; backend обязан возвращать значимость по тем же правилам. */
export const SIG_CRITERIA: Record<SignificanceLevel, { range: string; text: string }> = {
  high: { range: 'от 0,50', text: 'Несколько признаков вашей компании напрямую связаны с событием. Стоит принять решение или проверить расчёты в ближайшее время.' },
  medium: { range: '0,25–0,49', text: 'Связь есть через один-два признака. Учтите при планировании, срочных действий обычно не требуется.' },
  low: { range: 'ниже 0,25', text: 'Прямой связи с профилем почти нет. Событие показано для сведения.' },
};

/** Знак значимости: три столбца «уровня сигнала», заполнено 1–3. Смысл дублируется словом. */
export function SignalMark({ level }: { level: SignificanceLevel }) {
  const n = level === 'high' ? 3 : level === 'medium' ? 2 : 1;
  return (
    <span className={`signal signal--${level}`} aria-hidden>
      {[1, 2, 3].map((i) => <i key={i} className={i <= n ? 'on' : ''} />)}
    </span>
  );
}

export function SignificanceBadge({ level }: { level: SignificanceLevel }) {
  return (
    <span className={`sig sig--${level}`} aria-label={`Значимость: ${SIG_LABEL[level]}`}>
      <SignalMark level={level} />
      {SIG_LABEL[level]}
    </span>
  );
}

/** Плитка-иконка в углу карточки (как на тёмных карточках сайта банка). */
export function IconTile({ tone = 'neutral', children }: { tone?: 'neutral' | 'cyan' | 'lime' | 'salmon' | 'peach' | 'blue' | 'violet' | 'red' | 'dark'; children: ReactNode }) {
  return <span className={`icon-tile icon-tile--${tone}`} aria-hidden>{children}</span>;
}

/** Небольшая «таблетка» с белой подложкой на цветных карточках. */
export function Pill({ children, tone }: { children: ReactNode; tone?: 'warn' | 'info' | 'dark' }) {
  return <span className={`pill${tone ? ` pill--${tone}` : ''}`}>{children}</span>;
}

/* ---------------- Мелкие элементы ---------------- */

export function Badge({ tone = 'neutral', children, icon }: { tone?: 'neutral' | 'info' | 'warning' | 'success' | 'ai' | 'error'; children: ReactNode; icon?: ReactNode }) {
  return (
    <span className={`badge badge--${tone}`}>
      {icon}
      {children}
    </span>
  );
}

export function Chip({ children, muted, className = '' }: { children: ReactNode; muted?: boolean; className?: string }) {
  return <span className={`feature-chip${muted ? ' feature-chip--muted' : ''} ${className}`}>{children}</span>;
}

export function Skeleton({ h = 16, w = '100%', r = 8 }: { h?: number; w?: number | string; r?: number }) {
  return <span className="skeleton" style={{ height: h, width: w, borderRadius: r }} aria-hidden />;
}

export function FeedSkeleton({ count = 4 }: { count?: number }) {
  return (
    <div className="feed-list" aria-busy="true">
      <p className="sr-only" role="status">Загружаем события…</p>
      {Array.from({ length: count }).map((_, i) => (
        <div className="topic-card topic-card--skeleton" key={i} aria-hidden>
          <Skeleton h={14} w={140} r={4} />
          <Skeleton h={26} w="80%" />
          <Skeleton h={16} w="55%" />
        </div>
      ))}
    </div>
  );
}

/* ---------------- Пустые состояния ---------------- */

export function StateBlock({ icon, title, text, action, tone = 'neutral' }: { icon: ReactNode; title: string; text?: string; action?: ReactNode; tone?: 'neutral' | 'error' }) {
  return (
    <div className={`state state--${tone}`} role={tone === 'error' ? 'alert' : undefined}>
      <span className="state__icon">{icon}</span>
      <h3 className="h3">{title}</h3>
      {text && <p className="muted">{text}</p>}
      {action}
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  const [more, setMore] = useState(false);
  return (
    <StateBlock
      tone="error"
      icon={<AlertCircle size={24} aria-hidden />}
      title="Не удалось загрузить события"
      text="Это временная проблема сервиса. Попробуйте ещё раз — сохранённые ранее данные не потеряны."
      action={
        <>
          <Button variant="secondary" onClick={onRetry}>Повторить</Button>
          <button type="button" className="link-btn" onClick={() => setMore((v) => !v)} aria-expanded={more}>Подробнее</button>
          {more && <code className="state__details">{message}</code>}
        </>
      }
    />
  );
}

/* ---------------- Поповер и модалка ---------------- */

export function usePopover() {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false); };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);
  return { open, setOpen, ref };
}

export function Modal({ title, onClose, children, footer }: { title: string; onClose: () => void; children: ReactNode; footer?: ReactNode }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => { document.removeEventListener('keydown', onKey); document.body.style.overflow = prev; };
  }, [onClose]);
  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={title}>
        <header className="modal__head">
          <h2 className="h3">{title}</h2>
          <IconButton label="Закрыть" onClick={onClose}><X size={20} /></IconButton>
        </header>
        <div className="modal__body">{children}</div>
        {footer && <footer className="modal__foot">{footer}</footer>}
      </div>
    </div>
  );
}
