import type { CompanyProfile } from '../types';
import { initialsOf } from '../data/companies';

/* Фирменные элементы прототипа. Знак «А» Альфа-Банка взят из открытого пакета дизайн-системы банка
   `@alfalab/icons-logotype` (лицензия MIT, иконка AlfaBankLIcon); красная плитка вокруг него и логотипы демо-компаний
   нарисованы командой. */

/** Оригинальный контур знака Альфа-Банка (viewBox 21×32) из `@alfalab/icons-logotype`. */
const ALFA_GLYPH = "M10.471 4.886l-3.081 8.89h6.106l-2.91-8.89h-.115zM10.7 0c2.474 0 3.196 1.432 3.822 3.245L21 22h-4.794l-1.455-4.459H6.077L4.508 22H0L6.798 3.245C7.458 1.426 8.226 0 10.7 0zM0 27h21v5H0v-5z";

/** Знак Альфа-Бизнеса: красная скруглённая плитка с оригинальной белой «А». */
export function AlfaLogo({ size = 40, className = '', title }: { size?: number; className?: string; title?: string }) {
  return (
    <svg
      className={`alfa-logo ${className}`}
      width={size}
      height={size}
      viewBox="0 0 40 40"
      role={title ? 'img' : undefined}
      aria-label={title}
      aria-hidden={title ? undefined : true}
      focusable="false"
    >
      <rect width="40" height="40" rx="10" fill="#ef3124" />
      <path fill="#fff" fillRule="evenodd" clipRule="evenodd" transform="translate(12.4 8.4) scale(0.7)" d={ALFA_GLYPH} />
    </svg>
  );
}

/** Знак + вордмарк «Альфа-Бизнес». Подпись берёт класс brand__name, чтобы на телефоне её можно было скрыть. */
export function BrandLockup({ size = 40, name = 'Альфа-Бизнес', className = '' }: { size?: number; name?: string; className?: string }) {
  return (
    <span className={`lockup ${className}`}>
      <AlfaLogo size={size} />
      <span className="brand__name lockup__name">{name}</span>
    </span>
  );
}

/* ---------------- Логотипы демо-компаний ---------------- */

type LogoDef = { bg: string; draw: JSX.Element };

/** Нарисованные знаки синтетических компаний: узнаваемая цветная плитка вместо инициалов. */
const LOGOS: Record<string, LogoDef> = {
  /* «Уралметпром»: графитовая плитка, раскалённые горные пики Урала */
  SYNTHETIC_U_1042: {
    bg: '#2a2e35',
    draw: (
      <>
        <path d="M6 29 15.5 13l5 8.3L24 16l10 13z" fill="#ff7a2f" />
        <path d="M15.5 13 20.5 21.3 18 25.5 13 17.2z" fill="#ffb648" />
        <path d="M24 16 34 29h-5.5L22 19.4z" fill="#ffb648" />
        <rect x="6" y="30.5" width="28" height="2.5" rx="1.2" fill="#ffd27a" />
      </>
    ),
  },
  /* «АгроЭкспорт Юг»: зелёная плитка, золотой колос */
  SYNTHETIC_U_2218: {
    bg: '#17804a',
    draw: (
      <>
        <path d="M20 9v23" stroke="#ffd34d" strokeWidth="2.2" strokeLinecap="round" />
        {[12, 17, 22].map((y) => (
          <g key={y} fill="#ffd34d">
            <ellipse cx="16.6" cy={y + 1.6} rx="2.4" ry="4" transform={`rotate(-35 16.6 ${y + 1.6})`} />
            <ellipse cx="23.4" cy={y + 1.6} rx="2.4" ry="4" transform={`rotate(35 23.4 ${y + 1.6})`} />
          </g>
        ))}
        <ellipse cx="20" cy="9.6" rx="2" ry="3.4" fill="#ffe58a" />
      </>
    ),
  },
  /* «Нордфуд Ритейл»: лаймовая плитка с чёрной геометрической N */
  SYNTHETIC_U_3305: {
    bg: '#c8f53c',
    draw: <path d="M11 30V10h5.4l8 11.6V10H29v20h-5.3l-8.1-11.7V30z" fill="#111" />,
  },
};

const FALLBACK = ['#5b5bd6', '#0f8fbd', '#c2410c', '#9333ea', '#0e7f6e'];

/** Плитка-логотип компании. Для компаний без рисунка — цветная плитка с инициалами. */
export function CompanyLogo({ company, size = 40, className = '' }: { company: Pick<CompanyProfile, 'company_id' | 'name'> & { initials?: string }; size?: number; className?: string }) {
  const def = LOGOS[company.company_id];
  const radius = Math.round(size * 0.28);
  if (def) {
    return (
      <svg className={`company-logo ${className}`} width={size} height={size} viewBox="0 0 40 40" aria-hidden focusable="false">
        <rect width="40" height="40" rx={(radius / size) * 40} fill={def.bg} />
        {def.draw}
      </svg>
    );
  }
  const n = [...company.company_id].reduce((a, ch) => a + ch.charCodeAt(0), 0);
  return (
    <span
      className={`company-logo company-logo--text ${className}`}
      aria-hidden
      style={{ width: size, height: size, borderRadius: radius, background: FALLBACK[n % FALLBACK.length], fontSize: Math.round(size * 0.36) }}
    >
      {initialsOf(company)}
    </span>
  );
}
