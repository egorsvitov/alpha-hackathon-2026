import type {
  Claim, CompanyProfile, DemoSettings, Evidence, FeedItem, Insight, NewsEvent, QaAnswer,
  Relevance, RelevanceFactor, Role, TraceStep, UnknownItem,
} from '../../types';
import { COMPANIES, featureEvidence, featureLabel, getCompany, roleLabel } from '../../data/companies';
import { EVENT_DEFS, EVIDENCE, TOPIC_LABELS, type Driver, type EventDef } from '../../data/events';
import { IMPACTS } from '../../data/impacts';
import { OTHER_TEMPLATE, TOPIC_TEMPLATES, classify } from './topics';

/* Мок-«backend»: те же функции и формы ответов, что будут у реального API.
   Ранжирование — объяснимые правила (вклад каждого фактора возвращается вместе с баллом), без LLM. */

export const DEMO_TODAY = '2026-10-20';
const STORAGE_KEY = 'alfa-news-demo-user-events-v1';

export const settings: DemoSettings = { latency: 'normal', failNext: false, budgetExhausted: false };

const LATENCY = { fast: 120, normal: 550, slow: 1800 } as const;
export const delay = () => new Promise<void>((r) => setTimeout(r, LATENCY[settings.latency]));

export class ApiError extends Error {}

async function guard() {
  await delay();
  if (settings.failNext) {
    settings.failNext = false;
    throw new ApiError('Сервис временно недоступен (демо-ошибка)');
  }
}

/* ---------------------------- пользовательские события ---------------------------- */

interface StoredUserEvent { def: EventDef; evidence: Evidence[]; template_topic: string }
let userEvents: StoredUserEvent[] = [];
const dynamicEvidence: Record<string, Evidence> = {};

try {
  const raw = localStorage.getItem(STORAGE_KEY);
  if (raw) {
    userEvents = JSON.parse(raw) as StoredUserEvent[];
    userEvents.forEach((u) => u.evidence.forEach((e) => (dynamicEvidence[e.evidence_id] = e)));
  }
} catch {
  /* localStorage недоступен — работаем в памяти */
}

const persist = () => {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(userEvents));
  } catch {
    /* ignore */
  }
};

const allDefs = (): EventDef[] => [...userEvents.map((u) => u.def), ...EVENT_DEFS];
const findDef = (id: string) => allDefs().find((d) => d.event.event_id === id);
const evidenceById = (id: string, c: CompanyProfile): Evidence | undefined =>
  id.startsWith('cf:') ? featureEvidence(id.slice(3), c) : EVIDENCE[id] ?? dynamicEvidence[id];

/* ---------------------------- ранжирование ---------------------------- */

const ROLE_AFFINITY: Record<Role, Partial<Record<NewsEvent['topic'], number>>> = {
  ceo: { sanctions: 0.05, counterparty: 0.05, industry: 0.03 },
  cfo: { rates: 0.08, inflation: 0.03, trade: 0.02 },
  treasurer: { rates: 0.08, fx: 0.08 },
  chief_accountant: { inflation: 0.03, trade: 0.03 },
};

const daysBetween = (a: string, b: string) => Math.max(0, Math.round((Date.parse(b) - Date.parse(a)) / 86_400_000));

export function scoreEvent(def: EventDef, company: CompanyProfile, role: Role): Relevance & { matched: Driver[] } {
  const matched = def.drivers.filter((d) => company.feature_keys.includes(d.feature));
  const factors: RelevanceFactor[] = matched.map((d) => ({
    feature_key: d.feature,
    label: featureLabel(d.feature),
    contribution: d.weight,
    detail: d.mechanism,
  }));
  const base = matched.reduce((s, d) => s + d.weight, 0);

  const days = daysBetween(def.event.date, DEMO_TODAY);
  const fresh = +(0.08 * Math.exp(-days / 30)).toFixed(3);
  factors.push({ feature_key: 'meta:freshness', label: 'Свежесть события', contribution: fresh, detail: `событие вышло ${days} дн. назад` });

  const n = def.event.sources_count;
  const srcW = n >= 3 ? 0.04 : n === 2 ? 0.02 : 0;
  factors.push({ feature_key: 'meta:sources', label: 'Подтверждение источниками', contribution: srcW, detail: `${n} независимых публикаций` });

  let roleW = 0;
  if (base >= 0.1) {
    roleW = ROLE_AFFINITY[role][def.event.topic] ?? 0;
    if (roleW > 0) factors.push({ feature_key: 'meta:role', label: `Роль: ${roleLabel(role)}`, contribution: roleW, detail: 'тема входит в зону ответственности роли' });
  }

  const score = Math.min(1, +(base + fresh + srcW + roleW).toFixed(3));
  const level = score >= 0.5 ? 'high' : score >= 0.25 ? 'medium' : 'low';
  factors.sort((a, b) => b.contribution - a.contribution);

  const explanation = matched.length
    ? `Связано: ${[...matched].sort((a, b) => b.weight - a.weight).slice(0, 2).map((d) => d.phrase).join(', ')}`
    : 'Прямой связи с профилем компании не найдено';

  return { score, level, factors, explanation, matched };
}

/* ---------------------------- публичный API ---------------------------- */

export async function getCompanies() {
  await delay();
  return COMPANIES;
}

export async function getFeed(companyId: string, role: Role): Promise<FeedItem[]> {
  await guard();
  const company = getCompany(companyId);
  return allDefs()
    .map((d): FeedItem => {
      const r = scoreEvent(d, company, role);
      return {
        event: d.event,
        relevance: { score: r.score, level: r.level, factors: r.factors, explanation: r.explanation },
        why_short: r.explanation,
        has_bank_position: d.bank_claims.length > 0,
        profile_version: company.profile_version,
      };
    })
    .sort((a, b) => b.relevance.score - a.relevance.score || b.event.date.localeCompare(a.event.date));
}

const sortByRole = (claims: Claim[], role: Role) =>
  [...claims].sort((a, b) => Number(b.roles?.includes(role) ?? false) - Number(a.roles?.includes(role) ?? false));

const strip = (q: string) => q.replace(/^Демо-цитата\.\s*/, '');

function buildTrace(def: EventDef, company: CompanyProfile, rel: ReturnType<typeof scoreEvent>, unknownProducts: string[], claimsTotal: number, claimsLinked: number): TraceStep[] {
  const steps: Omit<TraceStep, 'n'>[] = [];
  steps.push({ tool: 'plan', summary: 'Определены нужные данные: профиль компании, события, аналитика банка', result: 'План из 5 шагов, лимит повторов — 2', status: 'ok' });
  steps.push({
    tool: 'get_company_profile',
    summary: `Профиль ${company.company_id}`,
    result: `Версия ${company.profile_version}; признаков для ранжирования: ${company.feature_keys.length}${unknownProducts.length ? `; неизвестно: ${unknownProducts.join(', ')}` : ''}`,
    status: 'ok',
  });
  steps.push({ tool: 'search_news', summary: `Новости по теме «${TOPIC_LABELS[def.event.topic]}»`, result: `${def.event.sources_count} публикаций → ${def.event.dedup_note ?? 'одно событие'}`, status: 'ok' });

  const tpl = def.event.user_uploaded ? userEvents.find((u) => u.def.event.event_id === def.event.event_id) : undefined;
  const t = tpl ? [...TOPIC_TEMPLATES, OTHER_TEMPLATE].find((x) => x.topic === def.event.topic) ?? OTHER_TEMPLATE : undefined;

  if (def.event.user_uploaded && t) {
    steps.push({ tool: 'search_bank_research', summary: `Запрос: «${t.first_query}»`, result: '0 релевантных фрагментов — неполнота', status: 'retry' });
    if (def.bank_ids.length) {
      steps.push({ tool: 'search_bank_research', summary: `Повторный поиск: «${t.retry_query}»`, result: `${def.bank_ids.length} фрагм. аналитики банка найдено`, status: 'ok' });
    } else {
      steps.push({ tool: 'search_bank_research', summary: `Повторный поиск: «${t.retry_query}»`, result: 'Релевантной аналитики банка нет — позиция банка не формируется', status: 'warn' });
    }
  } else if (def.bank_ids.length === 0) {
    steps.push({ tool: 'search_bank_research', summary: 'Аналитика банка по теме события', result: '0 релевантных фрагментов', status: 'retry' });
    steps.push({ tool: 'search_bank_research', summary: 'Повторный поиск по смежным темам (commodities, макро)', result: 'по-прежнему нет — позиция банка не формируется, не выдумывается', status: 'warn' });
  } else {
    steps.push({ tool: 'search_bank_research', summary: 'Аналитика банка по теме события', result: `${def.bank_ids.length} фрагм. (с учётом даты публикации)`, status: 'ok' });
  }

  if (def.event.event_id === 'e2_eu_steel_quota') {
    steps.push({ tool: 'check', summary: 'Проверка согласованности источников', result: 'Расхождение по срокам (n_e2_2 и n_e2_3) → повторный поиск смежных источников → подтверждения нет → сроки помечены «не подтверждено»', status: 'retry' });
  }
  if (rel.matched.some((d) => d.feature.startsWith('counterparty:'))) {
    steps.push({ tool: 'get_counterparties', summary: 'Сеть контрагентов по платежам (PAYER → BENEFICIARY)', result: 'Найден поставщик из новости: 21% наблюдаемого исходящего потока', status: 'ok' });
  }
  steps.push({
    tool: 'verify',
    summary: 'Проверка обоснованности: числа, даты, ссылки ↔ утверждения',
    result: `${claimsLinked} из ${claimsTotal} утверждений подтверждены источниками; остальные помечены как условные`,
    status: claimsLinked < claimsTotal ? 'warn' : 'ok',
  });
  steps.push({ tool: 'compose', summary: 'Сборка карточки (факты / позиция банка / вывод AI разделены)', result: 'JSON по контракту, статус ok', status: 'ok' });
  return steps.map((s, i) => ({ ...s, n: i + 1 }));
}

export async function getInsight(eventId: string, companyId: string, role: Role): Promise<Insight> {
  await guard();
  const def = findDef(eventId);
  if (!def) throw new ApiError('Событие не найдено');
  const company = getCompany(companyId);
  const rel = scoreEvent(def, company, role);
  const impact = IMPACTS[`${eventId}|${companyId}`];
  const topicTpl = [...TOPIC_TEMPLATES, OTHER_TEMPLATE].find((t) => t.topic === def.event.topic) ?? OTHER_TEMPLATE;

  let why: Claim[]; let risks: Claim[]; let opps: Claim[]; let actions: Claim[]; let unknowns: UnknownItem[];
  let ai: Insight['ai_conclusion'];
  let status: Insight['status'] = 'ok';

  if (impact) {
    ({ why, risks, opportunities: opps, next_actions: actions, unknowns } = impact);
    ai = impact.ai;
  } else if (rel.matched.length) {
    const firstNews = def.news_ids[0] ? [def.news_ids[0]] : [];
    why = [...rel.matched].sort((a, b) => b.weight - a.weight).slice(0, 3).map((d) => ({
      text: `Связано с признаком «${featureLabel(d.feature)}»: ${d.mechanism}.`,
      evidence_ids: firstNews,
      features: [d.feature],
      conditional: true,
    }));
    const feats = rel.matched.map((d) => d.feature);
    risks = topicTpl.generic.risks.map((t) => ({ text: t, evidence_ids: firstNews, features: feats, conditional: true }));
    opps = topicTpl.generic.opportunities.map((t) => ({ text: t, evidence_ids: def.bank_ids.slice(0, 1), features: feats, conditional: true }));
    actions = topicTpl.generic.actions.map((t) => ({ text: t, evidence_ids: [], features: feats }));
    unknowns = [{
      text: 'Масштаб влияния зависит от условий конкретных договоров и структуры бизнеса: профиль фиксирует лишь наличие признаков.',
      how_to_clarify: 'Запросить у профильного подразделения данные о договорах и объёмах, связанных с событием.',
      question: 'Какие данные нужны, чтобы оценить масштаб влияния этого события?',
    }];
    ai = {
      text: `Для компании «${company.name}» событие связано с ${rel.matched.length > 1 ? 'несколькими признаками профиля' : 'одним признаком профиля'}; вывод общий и условный — детальная оценка требует данных о договорах и объёмах.`,
      evidence_ids: firstNews,
      features: feats,
    };
  } else {
    if (def.event.topic === 'other') status = 'insufficient_data';
    why = [{
      text: 'Прямой связи события с признаками профиля (отрасль, продукты, регион, контрагенты) не найдено.',
      evidence_ids: [],
      features: [],
    }];
    risks = []; opps = []; actions = [];
    unknowns = [{
      text: status === 'insufficient_data'
        ? 'Тема события не определена, подходящих признаков профиля и аналитики банка не найдено — персональный вывод не формируется.'
        : 'В профиле нет признаков, через которые событие могло бы повлиять на компанию.',
      how_to_clarify: status === 'insufficient_data'
        ? 'Уточните формулировку события или загрузите полный текст новости.'
        : 'Если у компании есть связи с затронутой отраслью (поставщики, покупатели), добавьте их в профиль.',
      question: 'Почему это событие не связано с моей компанией?',
    }];
    ai = null;
  }

  const bankClaims = def.bank_claims.length
    ? def.bank_claims
    : [];

  /* ---- сбор источников в порядке цитирования ---- */
  const claimPool: Claim[] = [...def.facts, ...why, ...risks, ...opps, ...actions, ...bankClaims];
  const orderedIds: string[] = [...def.news_ids];
  const push = (id: string) => { if (!orderedIds.includes(id)) orderedIds.push(id); };
  claimPool.forEach((c) => c.evidence_ids.forEach(push));
  (ai?.evidence_ids ?? []).forEach(push);
  def.bank_ids.forEach(push);
  const featureKeys = new Set<string>();
  claimPool.forEach((c) => c.features?.forEach((f) => featureKeys.add(f)));
  ai?.features.forEach((f) => featureKeys.add(f));
  featureKeys.forEach((k) => orderedIds.push(`cf:${k}`));
  const evidence = orderedIds.map((id) => evidenceById(id, company)).filter((e): e is Evidence => Boolean(e));

  const unknownProducts = Object.entries(company.products)
    .filter(([, v]) => v === null)
    .map(([k]) => ({ leasing: 'лизинг', rko: 'РКО', guarantees: 'гарантии', loans: 'кредиты', deposits: 'депозиты', payroll: 'зарплатный проект', factoring: 'факторинг' } as Record<string, string>)[k] ?? k);
  const claimsTotal = claimPool.length;
  const claimsLinked = claimPool.filter((c) => c.evidence_ids.length > 0).length;

  const runId = `r_${(eventId + companyId + role).split('').reduce((a, ch) => (a * 31 + ch.charCodeAt(0)) >>> 0, 7).toString(16)}`;

  return {
    run_id: runId,
    company_id: companyId,
    profile_version: company.profile_version,
    user_role: role,
    status,
    generated_by: settings.budgetExhausted ? 'cached' : 'live',
    event: def.event,
    relevance: { score: rel.score, level: rel.level, factors: rel.factors, explanation: rel.explanation },
    what_happened: { summary: def.summary, facts: def.facts },
    why_it_matters: why,
    risks,
    opportunities: opps,
    next_actions: sortByRole(actions, role),
    bank_position: bankClaims.length ? { as_of: def.bank_as_of, claims: bankClaims } : null,
    ai_conclusion: ai,
    unknowns,
    evidence,
    trace: buildTrace(def, company, rel, unknownProducts, claimsTotal, claimsLinked),
  };
}

/* ---------------------------- уточняющий вопрос ---------------------------- */

const pick = (ins: Insight, ids: string[]) => ins.evidence.filter((e) => ids.includes(e.evidence_id) && e.type !== 'company_feature');

export async function ask(insight: Insight, question: string): Promise<QaAnswer> {
  await guard();
  const company = getCompany(insight.company_id);
  const q = question.toLowerCase();
  const fromCache = settings.budgetExhausted;
  const nonFeature = insight.evidence.filter((e) => e.type !== 'company_feature');

  const match = insight.unknowns.find((u) => u.question && u.question.toLowerCase() === q);
  if (match) {
    const ids = insight.what_happened.facts[0]?.evidence_ids ?? [];
    return {
      text: `Данных в профиле для точного ответа нет. ${match.text} Как уточнить: ${match.how_to_clarify}`,
      evidence: pick(insight, ids),
      evidence_ids: ids,
      did_extra_search: false,
      from_cache: fromCache,
      insufficient: true,
    };
  }

  if (/кредит|долг|ставк|рефинанс|лизинг|заём|займ/.test(q)) {
    const has = company.products.loans;
    const ids = ['b_key', 'n_e1_1'].filter((id) => insight.evidence.some((e) => e.evidence_id === id));
    return {
      text: has
        ? `У «${company.name}» есть кредитные продукты, но тип ставки, остаток и график погашения в профиле не указаны, поэтому количественно оценить эффект нельзя. Качественно: пока ключевая ставка остаётся высокой, стоимость обслуживания долга с плавающей ставкой не снизится. Следующий шаг — получить у казначейства реестр кредитов с типом ставки.`
        : `В профиле «${company.name}» кредитных продуктов нет (поле CL_PRODUCT_LOA_MARK = 0), поэтому прямого влияния на стоимость обслуживания долга не ожидается. Косвенно ставка влияет на потребительский спрос и доходность размещений.`,
      evidence: pick(insight, ids).length ? pick(insight, ids) : nonFeature.slice(0, 2),
      evidence_ids: ids,
      did_extra_search: false,
      from_cache: fromCache,
      insufficient: false,
    };
  }
  if (/валют|курс|хедж|форвард/.test(q)) {
    const ids = ['n_e3_1', 'b_tact'];
    return {
      text: company.fts.flag
        ? `В профиле есть запись ФТС (страна: ${company.fts.country}), значит внешнеторговые операции у компании есть. Но валюта контрактов и сроки платежей неизвестны, поэтому достоверно оценить валютную позицию нельзя. Банк в тактическом взгляде советует защищать маржу и ликвидность, а открытую валютную позицию держать осознанно.`
        : 'Записей ФТС у компании нет, прямая валютная экспозиция по внешней торговле в данных не подтверждена. Это не означает её отсутствие: могут быть импортные закупки через посредников.',
      evidence: ids.map((id) => EVIDENCE[id]).filter(Boolean),
      evidence_ids: ids,
      did_extra_search: true,
      extra_search_note: 'Выполнен дополнительный поиск: «курс рубля прогноз», «тактический взгляд валютный риск»',
      from_cache: fromCache,
      insufficient: false,
    };
  }
  if (/контрагент|поставщик|покупател|сеть/.test(q)) {
    const list = company.counterparties.map((c) => `${c.name} — ${c.share_pct}% ${c.direction === 'out' ? 'исходящего' : 'входящего'} наблюдаемого потока`).join('; ');
    return {
      text: `Крупнейшие связи по платёжной сети: ${list}. Доли считаются отдельно по входящим и исходящим платежам и описывают только наблюдаемый поток.`,
      evidence: [featureEvidence('counterparty:coke_resource', company)].filter(() => company.feature_keys.includes('counterparty:coke_resource')),
      evidence_ids: [],
      did_extra_search: false,
      from_cache: fromCache,
      insufficient: false,
    };
  }
  if (/банк|позици|аналитик|мнени/.test(q)) {
    if (!insight.bank_position) {
      return {
        text: 'По этому событию позиции банка в доступных материалах нет. Я не формирую позицию банка самостоятельно: выводы выше — оценка модели на основе источников.',
        evidence: [],
        evidence_ids: [],
        did_extra_search: true,
        extra_search_note: 'Выполнен повторный поиск по аналитике банка: релевантных фрагментов не найдено',
        from_cache: fromCache,
        insufficient: true,
      };
    }
    const ids = insight.bank_position.claims.flatMap((c) => c.evidence_ids);
    return {
      text: `Позиция банка: ${insight.bank_position.claims.map((c) => c.text).join(' ')}`,
      evidence: pick(insight, ids),
      evidence_ids: ids,
      did_extra_search: false,
      from_cache: fromCache,
      insufficient: false,
    };
  }
  if (/сдела|шаг|действ|рекоменд|что дальше/.test(q)) {
    const list = insight.next_actions.slice(0, 3).map((a, i) => `${i + 1}) ${a.text}`).join(' ');
    const ids = insight.next_actions.slice(0, 3).flatMap((a) => a.evidence_ids);
    return {
      text: list ? `Для роли «${roleLabel(insight.user_role)}» сначала: ${list}` : 'Конкретных шагов для этого события не сформировано: связь с профилем слабая.',
      evidence: pick(insight, ids),
      evidence_ids: ids,
      did_extra_search: false,
      from_cache: fromCache,
      insufficient: !list,
    };
  }

  return {
    text: 'В подготовленных материалах прямого ответа на этот вопрос нет. Выполнен дополнительный поиск по новостям и аналитике банка, подтверждающих фрагментов не найдено — поэтому я не делаю вывод. Попробуйте уточнить вопрос или выберите подсказку.',
    evidence: [],
    evidence_ids: [],
    did_extra_search: true,
    extra_search_note: `Выполнен дополнительный поиск по запросу: «${question.slice(0, 80)}» — подтверждений не найдено`,
    from_cache: fromCache,
    insufficient: true,
  };
}

/* ---------------------------- загрузка новой новости ---------------------------- */

export async function ingestNews(title: string, text: string): Promise<NewsEvent> {
  await guard();
  const body = `${title}. ${text}`;
  const tpl = classify(body);
  const id = `u_${Date.now().toString(36)}`;
  const evId = `n_${id}_1`;
  const evidence: Evidence = {
    evidence_id: evId,
    type: 'synthetic',
    title,
    publisher: 'Загружено пользователем (демо)',
    date: DEMO_TODAY,
    quote: text.trim().slice(0, 320) || title,
    is_synthetic: true,
  };
  dynamicEvidence[evId] = evidence;

  const sentences = text.split(/(?<=[.!?])\s+/).filter(Boolean).slice(0, 3);
  const facts: Claim[] = (sentences.length ? sentences : [title]).map((s) => ({ text: s.trim(), evidence_ids: [evId] }));
  const bankClaims: Claim[] = tpl.bank_ids.map((b) => ({ text: strip(EVIDENCE[b].quote), evidence_ids: [b] }));

  const event: NewsEvent = {
    event_id: id,
    title,
    date: DEMO_TODAY,
    topic: tpl.topic,
    topic_label: TOPIC_LABELS[tpl.topic],
    sources_count: 1,
    is_synthetic: true,
    user_uploaded: true,
  };
  const def: EventDef = {
    event,
    drivers: tpl.drivers,
    news_ids: [evId],
    bank_ids: tpl.bank_ids,
    facts,
    summary: title,
    bank_as_of: tpl.bank_ids.length ? EVIDENCE[tpl.bank_ids[0]].date : null,
    bank_claims: bankClaims,
  };
  userEvents.unshift({ def, evidence: [evidence], template_topic: tpl.topic });
  persist();
  return event;
}

export function resetUserEvents() {
  userEvents = [];
  persist();
}

export async function sendFeedback(_insightRunId: string, _useful: boolean): Promise<void> {
  await delay();
}
