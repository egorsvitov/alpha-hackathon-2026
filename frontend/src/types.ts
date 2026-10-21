// Типы повторяют контракт из docs/frontend-api-contract.md.
// Контракт ещё не согласован с задачами 1, 4, 5 — при изменении правим только этот файл и слой api/.

export type Role = 'ceo' | 'cfo' | 'treasurer' | 'chief_accountant';
export type SignificanceLevel = 'high' | 'medium' | 'low';
export type Topic = 'rates' | 'sanctions' | 'fx' | 'trade' | 'inflation' | 'counterparty' | 'industry' | 'other';

export interface ProductFlags {
  rko: boolean | null;
  guarantees: boolean | null;
  loans: boolean | null;
  leasing: boolean | null;
  deposits: boolean | null;
  payroll: boolean | null;
  factoring: boolean | null;
}

export interface CounterpartyLink {
  name: string;
  direction: 'in' | 'out';
  /** доля наблюдаемого потока (входящего или исходящего), % */
  share_pct: number;
  note?: string;
}

/** Признаки профиля, которые попадают в агента (задача 4). null = данные неизвестны, это не «нет». */
export interface CompanyProfile {
  company_id: string;
  name: string;
  /** если нет — фронт возьмёт первые буквы названия */
  initials?: string;
  okved_code: string;
  okved_name: string;
  industry: string;
  stage: string;
  revenue_bn_rub: number;
  age_label: string;
  workers_label: string;
  region: string;
  city: string;
  products: ProductFlags;
  fts: { flag: boolean | null; country: string | null };
  group_id: string | null;
  counterparties: CounterpartyLink[];
  /** ключи признаков для ранжирования, см. data/features.ts */
  feature_keys: string[];
  profile_version: string;
  is_synthetic: true;
}

export interface NewsEvent {
  event_id: string;
  title: string;
  date: string;
  topic: Topic;
  topic_label: string;
  sources_count: number;
  dedup_note?: string;
  is_synthetic: boolean;
  user_uploaded?: boolean;
  /** синтетическое соответствие реальной новости обезличенной компании: показываем явно */
  synthetic_link?: string;
}

export type EvidenceType = 'news' | 'bank_research' | 'company_feature' | 'synthetic';

export interface Evidence {
  evidence_id: string;
  type: EvidenceType;
  title: string;
  publisher: string;
  date: string | null;
  doc?: string;
  page?: number;
  url?: string;
  quote: string;
  /** демо-текст, не материал банка/СМИ */
  is_synthetic: boolean;
}

/** Природа утверждения: факт из источника, вывод модели или гипотеза (условное допущение). */
export type ClaimKind = 'fact' | 'inference' | 'hypothesis';

export interface Claim {
  text: string;
  /** если не указано, фронт берёт по разделу: «что случилось» — факт, остальное — вывод; conditional → гипотеза */
  kind?: ClaimKind;
  evidence_ids: string[];
  /** ключи признаков клиента, повлиявших на вывод */
  features?: string[];
  conditional?: boolean;
  roles?: Role[];
}

export interface RelevanceFactor {
  feature_key: string;
  label: string;
  contribution: number;
  detail: string;
}

export interface Relevance {
  score: number;
  level: SignificanceLevel;
  factors: RelevanceFactor[];
  explanation: string;
  /** если у ранжирования нет числового вклада факторов, достаточно текстовых причин */
  reasons?: string[];
}

export interface UnknownItem {
  text: string;
  how_to_clarify?: string;
  question?: string;
}

export interface TraceStep {
  n: number;
  tool: string;
  summary: string;
  result: string;
  status: 'ok' | 'retry' | 'warn';
}

/** Сценарная количественная оценка эффекта. Показывается только при высокой значимости и всегда как сценарий с допущениями. */
export interface ImpactEstimate {
  metric: string;
  summary: string;
  assumptions: string[];
  evidence_ids: string[];
}

export interface Insight {
  run_id: string;
  company_id: string;
  profile_version: string;
  user_role: Role;
  status: 'ok' | 'insufficient_data';
  generated_by: 'live' | 'cached';
  event: NewsEvent;
  relevance: Relevance;
  what_happened: { summary: string; facts: Claim[] };
  why_it_matters: Claim[];
  risks: Claim[];
  opportunities: Claim[];
  next_actions: Claim[];
  impact_estimate?: ImpactEstimate | null;
  bank_position: { as_of: string | null; claims: Claim[] } | null;
  ai_conclusion: { text: string; evidence_ids: string[]; features: string[] } | null;
  unknowns: UnknownItem[];
  evidence: Evidence[];
  trace: TraceStep[];
}

export interface FeedItem {
  event: NewsEvent;
  relevance: Relevance;
  why_short: string;
  has_bank_position: boolean;
  /** условное — в ленте пересчёт идёт на лету */
  profile_version: string;
}

export interface QaAnswer {
  text: string;
  evidence: Evidence[];
  evidence_ids: string[];
  did_extra_search: boolean;
  extra_search_note?: string;
  from_cache: boolean;
  insufficient: boolean;
}

export type ActionKind = 'manager' | 'request' | 'callback';

export interface DemoUser {
  login: string;
  name: string;
}

export interface DemoSettings {
  latency: 'fast' | 'normal' | 'slow';
  failNext: boolean;
  budgetExhausted: boolean;
}
