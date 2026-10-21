import { useId, type ComponentType, type ReactNode } from 'react';
import type { Topic } from '../types';

/* Собственные иллюстрации для карточек: глянцевые «игрушечные» предметы в духе фирменного стиля банка.
   Нарисованы с нуля (градиенты и блики), ассеты банка не используются и не копируются. */

type Stop = [number, string];
const PAL: Record<string, Stop[]> = {
  gold: [[0, '#fff3c4'], [0.45, '#f9c94f'], [1, '#d08a1c']],
  teal: [[0, '#b6fbf2'], [0.5, '#35cfc2'], [1, '#14877f']],
  red: [[0, '#ff8d7d'], [0.5, '#f0392a'], [1, '#a8160c']],
  purple: [[0, '#d3a0ff'], [0.5, '#8d3ff0'], [1, '#5521b5']],
  blue: [[0, '#b4d2ff'], [0.5, '#4f88f2'], [1, '#2349b0']],
  green: [[0, '#d2f78a'], [0.5, '#6cc43c'], [1, '#2f7f18']],
  white: [[0, '#ffffff'], [1, '#d6daee']],
  steel: [[0, '#f6f8ff'], [0.5, '#b2bdd9'], [1, '#6a7696']],
  orange: [[0, '#ffd8a6'], [0.5, '#ff9a3c'], [1, '#d2601a']],
};

function useUid() {
  return useId().replace(/[^a-zA-Z0-9]/g, '');
}

function Defs({ u }: { u: string }) {
  return (
    <defs>
      {Object.entries(PAL).map(([name, stops]) => (
        <g key={name}>
          <linearGradient id={`${u}-${name}`} x1="0" y1="0" x2="1" y2="1">
            {stops.map(([o, c]) => <stop key={o} offset={o} stopColor={c} />)}
          </linearGradient>
          <linearGradient id={`${u}-${name}R`} x1="1" y1="1" x2="0" y2="0">
            {stops.map(([o, c]) => <stop key={o} offset={o} stopColor={c} />)}
          </linearGradient>
        </g>
      ))}
      <filter id={`${u}-blur`} x="-30%" y="-30%" width="160%" height="160%"><feGaussianBlur stdDeviation="4" /></filter>
    </defs>
  );
}

const Svg = ({ children, u }: { children: ReactNode; u: string }) => (
  <svg className="art" viewBox="0 0 240 200" role="img" aria-hidden focusable="false">
    <Defs u={u} />
    {children}
  </svg>
);

const Shadow = ({ u, cx = 120, cy = 186, rx = 78, ry = 8 }: { u: string; cx?: number; cy?: number; rx?: number; ry?: number }) => (
  <ellipse cx={cx} cy={cy} rx={rx} ry={ry} fill="rgba(0,0,0,.2)" filter={`url(#${u}-blur)`} />
);

function Coin({ u, cx, cy, r, g, label, rot = 0 }: { u: string; cx: number; cy: number; r: number; g: string; label: string; rot?: number }) {
  return (
    <g transform={`rotate(${rot} ${cx} ${cy})`}>
      <circle cx={cx} cy={cy} r={r} fill={`url(#${u}-${g})`} />
      <circle cx={cx} cy={cy} r={r * 0.8} fill={`url(#${u}-${g}R)`} stroke="rgba(255,255,255,.6)" strokeWidth={1.6} />
      <circle cx={cx} cy={cy} r={r * 0.9} fill="none" stroke="rgba(255,255,255,.35)" strokeWidth={1.2} strokeDasharray="2 3.2" />
      <text x={cx} y={cy + r * 0.32} textAnchor="middle" fontSize={r * 1.0} fontWeight={800} fontFamily="Inter Variable, Arial, sans-serif" fill="rgba(70,35,0,.5)">{label}</text>
      <text x={cx - 0.9} y={cy + r * 0.32 - 0.9} textAnchor="middle" fontSize={r * 1.0} fontWeight={800} fontFamily="Inter Variable, Arial, sans-serif" fill="rgba(255,255,255,.85)">{label}</text>
      <ellipse cx={cx - r * 0.36} cy={cy - r * 0.5} rx={r * 0.34} ry={r * 0.14} fill="rgba(255,255,255,.6)" transform={`rotate(-28 ${cx - r * 0.36} ${cy - r * 0.5})`} />
    </g>
  );
}

function Spark({ u, cx, cy, s, g = 'red' }: { u: string; cx: number; cy: number; s: number; g?: string }) {
  return (
    <g transform={`translate(${cx} ${cy}) scale(${s})`}>
      <path d="M0,-1 C0.1,-0.28 0.28,-0.1 1,0 C0.28,0.1 0.1,0.28 0,1 C-0.1,0.28 -0.28,0.1 -1,0 C-0.28,-0.1 -0.1,-0.28 0,-1 Z" fill={`url(#${u}-${g})`} />
      <path d="M0,-1 C0.06,-0.4 0.14,-0.2 0.4,-0.1 C0.1,-0.2 0.05,-0.45 0,-1 Z" fill="rgba(255,255,255,.55)" />
    </g>
  );
}

const gloss = (x: number, y: number, w: number, h: number, rx: number) => (
  <rect x={x + 3} y={y + 3} width={w - 6} height={h * 0.42} rx={rx - 2} fill="rgba(255,255,255,.28)" />
);

/* ---------------- Ставки и кредит: калькулятор с процентом ---------------- */
export function ArtRates() {
  const u = useUid();
  return (
    <Svg u={u}>
      <Shadow u={u} />
      <g transform="rotate(8 166 96)">
        <rect x="116" y="16" width="100" height="156" rx="20" fill={`url(#${u}-white)`} />
        <rect x="116" y="16" width="100" height="156" rx="20" fill="none" stroke="rgba(120,130,170,.35)" strokeWidth="2" />
        <rect x="126" y="28" width="80" height="48" rx="10" fill={`url(#${u}-purple)`} />
        <text x="166" y="66" textAnchor="middle" fontSize="40" fontWeight="800" fontFamily="Inter Variable, Arial" fill="#ff8ae6">%</text>
        {[0, 1].flatMap((i) => [0, 1, 2].map((j) => (
          <circle key={`${i}${j}`} cx={141 + i * 50} cy={98 + j * 24} r="11" fill="#e9ecf8" stroke="#c9cee6" strokeWidth="1.5" />
        )))}
        <rect x="132" y="30" width="50" height="8" rx="4" fill="rgba(255,255,255,.35)" />
      </g>
      <Coin u={u} cx={66} cy={134} r={40} g="gold" label="₽" rot={-8} />
      <Spark u={u} cx={52} cy={46} s={26} />
    </Svg>
  );
}

/* ---------------- Валюта: две монеты ---------------- */
export function ArtFx() {
  const u = useUid();
  return (
    <Svg u={u}>
      <Shadow u={u} />
      <Coin u={u} cx={90} cy={112} r={58} g="teal" label="$" rot={-10} />
      <Coin u={u} cx={156} cy={90} r={50} g="gold" label="₽" rot={8} />
      <path d="M170 150 q30 -6 34 -34" fill="none" stroke={`url(#${u}-red)`} strokeWidth="8" strokeLinecap="round" />
      <path d="M194 112 l12 10 l12 -12 z" fill={`url(#${u}-red)`} transform="rotate(10 206 118)" />
      <Spark u={u} cx={40} cy={44} s={20} g="purple" />
    </Svg>
  );
}

/* ---------------- Ограничения: щит ---------------- */
export function ArtShield() {
  const u = useUid();
  return (
    <Svg u={u}>
      <Shadow u={u} />
      <path d="M120 20 L186 44 V98 C186 140 158 166 120 180 C82 166 54 140 54 98 V44 Z" fill={`url(#${u}-red)`} />
      <path d="M120 38 L170 56 V98 C170 128 150 148 120 160 C90 148 70 128 70 98 V56 Z" fill={`url(#${u}-white)`} />
      <rect x="113" y="68" width="14" height="42" rx="7" fill={`url(#${u}-red)`} />
      <circle cx="120" cy="128" r="9" fill={`url(#${u}-red)`} />
      <path d="M72 44 L120 26 L120 40 L76 58 Z" fill="rgba(255,255,255,.3)" />
      <g transform="rotate(-8 46 162)">
        <rect x="14" y="150" width="84" height="20" rx="10" fill="#fff" />
        {[0, 1, 2, 3, 4].map((i) => <path key={i} d={`M${24 + i * 16} 150 l10 0 l-8 20 l-10 0 z`} fill={`url(#${u}-red)`} />)}
      </g>
      <Coin u={u} cx={196} cy={42} r={22} g="gold" label="€" rot={10} />
    </Svg>
  );
}

/* ---------------- Пошлины и зерно: колосья ---------------- */
function Ear({ u, x1, y1, x2, y2 }: { u: string; x1: number; y1: number; x2: number; y2: number }) {
  const n = 7;
  const ang = (Math.atan2(y2 - y1, x2 - x1) * 180) / Math.PI;
  return (
    <g>
      <line x1={x1} y1={y1} x2={x2} y2={y2} stroke="#c98a22" strokeWidth="5" strokeLinecap="round" />
      {Array.from({ length: n }).map((_, i) => {
        const t = 0.35 + (i / n) * 0.65;
        const px = x1 + (x2 - x1) * t; const py = y1 + (y2 - y1) * t;
        return [-1, 1].map((side) => (
          <ellipse key={`${i}${side}`} cx={px} cy={py} rx="6.5" ry="13" fill={`url(#${u}-gold)`} stroke="rgba(150,90,10,.35)" strokeWidth="1"
            transform={`rotate(${ang + 90 + side * 28} ${px} ${py}) translate(0 -9)`} />
        ));
      })}
    </g>
  );
}
export function ArtWheat() {
  const u = useUid();
  return (
    <Svg u={u}>
      <Shadow u={u} />
      <Ear u={u} x1={120} y1={176} x2={76} y2={46} />
      <Ear u={u} x1={120} y1={176} x2={122} y2={26} />
      <Ear u={u} x1={120} y1={176} x2={168} y2={46} />
      <g transform="rotate(-6 120 140)">
        <rect x="96" y="128" width="48" height="20" rx="10" fill={`url(#${u}-red)`} />
        <rect x="100" y="130" width="40" height="6" rx="3" fill="rgba(255,255,255,.4)" />
      </g>
      <Coin u={u} cx={184} cy={142} r={34} g="blue" label="%" rot={8} />
      <Spark u={u} cx={42} cy={140} s={16} g="orange" />
    </Svg>
  );
}

/* ---------------- Контрагенты: коробка с тревогой ---------------- */
export function ArtBox() {
  const u = useUid();
  return (
    <Svg u={u}>
      <Shadow u={u} />
      <polygon points="120,44 188,76 120,108 52,76" fill={`url(#${u}-orange)`} />
      <polygon points="52,76 120,108 120,176 52,144" fill={`url(#${u}-orangeR)`} style={{ filter: 'brightness(.92)' }} />
      <polygon points="188,76 120,108 120,176 188,144" fill={`url(#${u}-orangeR)`} style={{ filter: 'brightness(.72)' }} />
      <polygon points="120,44 134,50.6 66,82.6 52,76" fill="rgba(255,255,255,.35)" />
      <polygon points="66,98 104,116 104,138 66,120" fill="rgba(255,255,255,.88)" />
      <rect x="72" y="106" width="24" height="4" rx="2" transform="rotate(25 72 106)" fill="#e7a468" />
      <circle cx="176" cy="46" r="24" fill={`url(#${u}-red)`} />
      <rect x="172" y="32" width="8" height="18" rx="4" fill="#fff" />
      <circle cx="176" cy="58" r="4.6" fill="#fff" />
      <Coin u={u} cx={46} cy={156} r={22} g="gold" label="₽" rot={-12} />
    </Svg>
  );
}

/* ---------------- Инфляция: корзина ---------------- */
export function ArtBasket() {
  const u = useUid();
  return (
    <Svg u={u}>
      <Shadow u={u} />
      <path d="M84 86 C84 34 156 34 156 86" fill="none" stroke={`url(#${u}-red)`} strokeWidth="9" strokeLinecap="round" />
      <circle cx="102" cy="70" r="17" fill={`url(#${u}-green)`} />
      <circle cx="132" cy="64" r="15" fill={`url(#${u}-gold)`} />
      <rect x="116" y="42" width="5" height="10" rx="2" fill="#2f7f18" transform="rotate(18 118 46)" />
      <path d="M56 92 H184 L168 164 Q165 176 154 176 H86 Q75 176 72 164 Z" fill={`url(#${u}-red)`} />
      <rect x="48" y="84" width="144" height="16" rx="8" fill={`url(#${u}-redR)`} />
      {[0, 1, 2, 3].map((i) => <line key={i} x1={88 + i * 22} y1="104" x2={92 + i * 20} y2="168" stroke="rgba(255,255,255,.4)" strokeWidth="4" strokeLinecap="round" />)}
      <line x1="66" y1="132" x2="174" y2="132" stroke="rgba(255,255,255,.4)" strokeWidth="4" strokeLinecap="round" />
      <g transform="rotate(10 190 56)">
        <rect x="160" y="30" width="60" height="42" rx="12" fill="#fff" />
        <path d="M190 62 V42 M181 50 l9 -10 l9 10" fill="none" stroke={`url(#${u}-red)`} strokeWidth="6" strokeLinecap="round" strokeLinejoin="round" />
      </g>
    </Svg>
  );
}

/* ---------------- Отрасль: слитки ---------------- */
function Ingot({ u, x, y, w = 92, h = 44 }: { u: string; x: number; y: number; w?: number; h?: number }) {
  return (
    <g>
      <polygon points={`${x + 14},${y} ${x + w - 14},${y} ${x + w},${y + h * 0.5} ${x},${y + h * 0.5}`} fill={`url(#${u}-steel)`} />
      <rect x={x} y={y + h * 0.5} width={w} height={h * 0.5} rx="6" fill={`url(#${u}-steelR)`} style={{ filter: 'brightness(.88)' }} />
      <polygon points={`${x + 22},${y + 5} ${x + 44},${y + 5} ${x + 34},${y + h * 0.42} ${x + 12},${y + h * 0.42}`} fill="rgba(255,255,255,.55)" />
    </g>
  );
}
export function ArtIngots() {
  const u = useUid();
  return (
    <Svg u={u}>
      <Shadow u={u} />
      <Ingot u={u} x={26} y={130} />
      <Ingot u={u} x={122} y={130} />
      <Ingot u={u} x={74} y={96} />
      <Ingot u={u} x={74} y={62} />
      <Spark u={u} cx={186} cy={48} s={24} />
      <Coin u={u} cx={196} cy={150} r={26} g="teal" label="₽" rot={10} />
    </Svg>
  );
}

/* ---------------- Прочее: звезда ---------------- */
export function ArtSpark() {
  const u = useUid();
  return (
    <Svg u={u}>
      <Shadow u={u} />
      <Spark u={u} cx={110} cy={96} s={74} />
      <Spark u={u} cx={52} cy={44} s={22} g="purple" />
      <Spark u={u} cx={190} cy={56} s={14} g="blue" />
      <Coin u={u} cx={170} cy={142} r={32} g="gold" label="₽" rot={8} />
    </Svg>
  );
}

/* ---------------- Диаграмма: виджет и стартовый экран ---------------- */
export function ArtChart() {
  const u = useUid();
  const bars: [number, number, string][] = [[44, 64, 'purple'], [94, 98, 'blue'], [144, 138, 'red']];
  return (
    <Svg u={u}>
      <Shadow u={u} rx={90} />
      {bars.map(([x, h, g]) => (
        <g key={x}>
          <rect x={x} y={176 - h} width="42" height={h} rx="14" fill={`url(#${u}-${g})`} />
          {gloss(x, 176 - h, 42, h, 14)}
        </g>
      ))}
      <Coin u={u} cx={165} cy={30} r={26} g="gold" label="₽" rot={-10} />
      <Spark u={u} cx={40} cy={40} s={20} />
      <Spark u={u} cx={214} cy={96} s={12} g="purple" />
    </Svg>
  );
}

/* ---------------- Вход: телефон с замком ---------------- */
export function ArtLogin() {
  const u = useUid();
  return (
    <Svg u={u}>
      <Shadow u={u} rx={70} />
      <rect x="62" y="20" width="116" height="156" rx="28" fill={`url(#${u}-purple)`} />
      {gloss(62, 20, 116, 156, 28)}
      <rect x="74" y="34" width="92" height="128" rx="18" fill={`url(#${u}-white)`} />
      <path d="M102 92 V80 a18 18 0 0 1 36 0 V92" fill="none" stroke={`url(#${u}-steel)`} strokeWidth="9" strokeLinecap="round" />
      <rect x="94" y="90" width="52" height="42" rx="13" fill={`url(#${u}-red)`} />
      <circle cx="120" cy="108" r="6" fill="#fff" />
      <rect x="117" y="110" width="6" height="14" rx="3" fill="#fff" />
      <Coin u={u} cx={190} cy={142} r={24} g="gold" label="₽" rot={8} />
      <Spark u={u} cx={36} cy={52} s={18} />
      <Spark u={u} cx={206} cy={44} s={12} g="blue" />
    </Svg>
  );
}

/* ---------------- Подбор по теме ---------------- */
export const TOPIC_ART: Record<Topic, ComponentType> = {
  rates: ArtRates,
  fx: ArtFx,
  sanctions: ArtShield,
  trade: ArtWheat,
  counterparty: ArtBox,
  inflation: ArtBasket,
  industry: ArtIngots,
  other: ArtSpark,
};

export function TopicArt({ topic }: { topic: Topic }) {
  const Art = TOPIC_ART[topic] ?? ArtSpark;
  return <Art />;
}

/** Иллюстрация для компании по отрасли (экран выбора). */
export function CompanyArt({ industry }: { industry: string }) {
  if (/металл/i.test(industry)) return <ArtIngots />;
  if (/зерн|агро/i.test(industry)) return <ArtWheat />;
  if (/рознич|продукт/i.test(industry)) return <ArtBasket />;
  return <ArtSpark />;
}
