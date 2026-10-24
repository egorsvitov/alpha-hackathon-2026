import type { CompanyProfile, Evidence, Role } from '../types';

// ВСЕ компании синтетические. Названия вымышлены, идентификаторы SYNTHETIC_U — демонстрационные.
// Поля и градации повторяют схему df_Uid_data_hack.csv. null = «неизвестно», не «нет».

export const COMPANIES: CompanyProfile[] = [
  {
    company_id: 'SYNTHETIC_U_1042',
    name: 'ООО «Уралметпром»',
    initials: 'УМ',
    okved_code: '24.10',
    okved_name: 'Производство чугуна, стали и ферросплавов',
    industry: 'Металлургия',
    stage: 'Зрелый бизнес',
    revenue_bn_rub: 62.4,
    age_label: 'Более 20 лет',
    workers_label: '5 000 – 10 000 человек',
    region: 'Челябинская область',
    city: 'Челябинск',
    products: { rko: true, guarantees: true, loans: true, leasing: true, deposits: false, payroll: true, factoring: false },
    fts: { flag: true, country: 'Казахстан' },
    group_id: 'GRP-0317',
    counterparties: [
      { name: 'ООО «КоксРесурс»', direction: 'out', share_pct: 21, note: 'поставщик коксующегося угля' },
      { name: 'АО «ЭнергоСбыт Урал»', direction: 'out', share_pct: 11, note: 'энергоснабжение' },
      { name: 'ООО «МеталлТрейд Холдинг»', direction: 'in', share_pct: 17, note: 'оптовый покупатель' },
      { name: 'АО «СтройИндустрия»', direction: 'in', share_pct: 9, note: 'покупатель проката' },
    ],
    feature_keys: [
      'product.loans', 'product.leasing', 'product.guarantees', 'product.rko',
      'fts', 'industry:metallurgy', 'counterparty:coke_resource', 'region:ural',
    ],
    profile_version: 'p-2026-10-20-a1',
    is_synthetic: true,
  },
  {
    company_id: 'SYNTHETIC_U_2218',
    name: 'АО «АгроЭкспорт Юг»',
    initials: 'АЮ',
    okved_code: '46.21',
    okved_name: 'Торговля оптовая зерном, семенами и кормами для животных',
    industry: 'Агро-торговля, экспорт зерна',
    stage: 'Растущий бизнес',
    revenue_bn_rub: 27.4,
    age_label: '10 – 20 лет',
    workers_label: '1 000 – 5 000 человек',
    region: 'Краснодарский край',
    city: 'Краснодар',
    products: { rko: true, guarantees: true, loans: false, leasing: null, deposits: false, payroll: true, factoring: true },
    fts: { flag: true, country: 'Турция' },
    group_id: null,
    counterparties: [
      { name: 'ООО «ЗерноТрейд Интернешнл»', direction: 'in', share_pct: 24, note: 'экспортный покупатель' },
      { name: 'АО «Южный Элеватор»', direction: 'in', share_pct: 15, note: 'хранение и отгрузка' },
      { name: 'ООО «АгроТранс»', direction: 'out', share_pct: 18, note: 'логистика' },
      { name: 'ООО «Новороссийск-Терминал»', direction: 'out', share_pct: 12, note: 'портовые услуги' },
    ],
    feature_keys: ['product.factoring', 'product.guarantees', 'product.rko', 'fts', 'industry:grain', 'region:south'],
    profile_version: 'p-2026-10-20-a1',
    is_synthetic: true,
  },
  {
    company_id: 'SYNTHETIC_U_3305',
    name: 'ПАО «Нордфуд Ритейл»',
    initials: 'НР',
    okved_code: '47.11',
    okved_name: 'Торговля розничная преимущественно пищевыми продуктами в неспециализированных магазинах',
    industry: 'Розничная торговля продуктами',
    stage: 'Зрелый бизнес',
    revenue_bn_rub: 41.8,
    age_label: '10 – 20 лет',
    workers_label: 'Более 10 000 человек',
    region: 'Санкт-Петербург',
    city: 'Санкт-Петербург',
    products: { rko: true, guarantees: false, loans: false, leasing: false, deposits: true, payroll: true, factoring: false },
    fts: { flag: false, country: null },
    group_id: null,
    counterparties: [
      { name: 'ООО «Агрохолдинг Север»', direction: 'out', share_pct: 16, note: 'поставщик продуктов' },
      { name: 'ООО «ЛогистикПро»', direction: 'out', share_pct: 10, note: 'складская логистика' },
    ],
    feature_keys: ['product.deposits', 'product.payroll', 'product.rko', 'industry:retail_food', 'region:nw'],
    profile_version: 'p-2026-10-20-a1',
    is_synthetic: true,
  },
];

export const ROLES: { id: Role; label: string; hint: string }[] = [
  { id: 'ceo', label: 'Генеральный директор', hint: 'стратегия, риски отрасли и контрагентов' },
  { id: 'cfo', label: 'Финансовый директор', hint: 'стоимость денег, ковенанты, ликвидность' },
  { id: 'treasurer', label: 'Руководитель казначейства', hint: 'курсы, лимиты, размещение средств' },
  { id: 'chief_accountant', label: 'Главный бухгалтер', hint: 'учёт, налоги, отчётность' },
];

export const initialsOf = (c: { name: string; initials?: string }) =>
  c.initials ?? (c.name.replace(/[«»"]/g, '').split(/\s+/).filter((w) => /^[A-Za-zА-Яа-яЁё]/.test(w) && !/^(ООО|АО|ПАО|ЗАО|ИП)$/.test(w)).slice(0, 2).map((w) => w[0].toUpperCase()).join('') || '??');

export const roleLabel = (r: Role) => ROLES.find((x) => x.id === r)?.label ?? r;

export const getCompany = (id: string) => COMPANIES.find((c) => c.company_id === id) ?? COMPANIES[0];

/* ---------- Признаки профиля: подписи и «происхождение» (поле датасета) ---------- */

interface FeatureDef {
  label: string;
  field: string;
  value: (c: CompanyProfile) => string;
}

const flag = (v: boolean | null) => (v === null ? 'не определено' : v ? '1 (есть)' : '0 (нет)');

export const FEATURES: Record<string, FeatureDef> = {
  'product.loans': { label: 'Кредитные продукты', field: 'CL_PRODUCT_LOA_MARK', value: (c) => flag(c.products.loans) },
  'product.leasing': { label: 'Лизинг', field: 'CL_PRODUCT_LEA_MARK', value: (c) => flag(c.products.leasing) },
  'product.guarantees': { label: 'Банковские гарантии', field: 'CL_PRODUCT_GAR_MARK', value: (c) => flag(c.products.guarantees) },
  'product.deposits': { label: 'Депозиты', field: 'CL_PRODUCT_DEP_MARK', value: (c) => flag(c.products.deposits) },
  'product.factoring': { label: 'Факторинг', field: 'FACTORING_MARK', value: (c) => flag(c.products.factoring) },
  'product.rko': { label: 'РКО', field: 'CL_PRODUCT_RKO_MARK', value: (c) => flag(c.products.rko) },
  'product.payroll': { label: 'Зарплатный проект', field: 'CL_PRODUCT_ZPP_MARK', value: (c) => flag(c.products.payroll) },
  fts: {
    label: 'Внешнеторговые данные (ФТС)',
    field: 'FTS_FLAG / FTS_COUNTRY_NAME',
    value: (c) => `${flag(c.fts.flag)}${c.fts.country ? `, страна: ${c.fts.country}` : ''}`,
  },
  'industry:metallurgy': { label: 'Отрасль: металлургия', field: 'MAINOKVED_CODE', value: (c) => `${c.okved_code} ${c.okved_name}` },
  'industry:grain': { label: 'Отрасль: зерно и агро-торговля', field: 'MAINOKVED_CODE', value: (c) => `${c.okved_code} ${c.okved_name}` },
  'industry:retail_food': { label: 'Отрасль: продуктовая розница', field: 'MAINOKVED_CODE', value: (c) => `${c.okved_code} ${c.okved_name}` },
  'counterparty:coke_resource': {
    label: 'Контрагент: «КоксРесурс»',
    field: 'PAYER → BENEFICIARY (платежи)',
    value: (c) => {
      const l = c.counterparties.find((x) => x.name.includes('КоксРесурс'));
      return l ? `${l.share_pct}% наблюдаемого исходящего потока` : 'не найден';
    },
  },
  'region:ural': { label: 'Регион: Урал', field: 'REGION (регистрация)', value: (c) => c.region },
  'region:south': { label: 'Регион: юг России', field: 'REGION (регистрация)', value: (c) => c.region },
  'region:nw': { label: 'Регион: Северо-Запад', field: 'REGION (регистрация)', value: (c) => c.region },
};

export const featureLabel = (k: string) => FEATURES[k]?.label ?? k;

export function featureEvidence(key: string, c: CompanyProfile): Evidence {
  const f = FEATURES[key];
  return {
    evidence_id: `cf:${key}`,
    type: 'company_feature',
    title: f?.label ?? key,
    publisher: 'Профиль компании',
    date: null,
    doc: f?.field,
    quote: `${f?.field ?? key} = ${f ? f.value(c) : '—'}`,
    is_synthetic: true,
  };
}

export function profileChips(c: CompanyProfile): { label: string; known: boolean }[] {
  const p = c.products;
  const prod = (label: string, v: boolean | null) =>
    v === null ? { label: `${label}: неизвестно`, known: false } : { label: `${label}: ${v ? 'есть' : 'нет'}`, known: true };
  return [
    { label: `Отрасль: ${c.industry}`, known: true },
    { label: `Выручка: ${(c.revenue_bn_rub?.toLocaleString('ru-RU') ?? 'Неизвестно')} млрд ₽`, known: c.revenue_bn_rub != null },
    { label: `Регион: ${c.region}`, known: true },
    { label: `Возраст: ${c.age_label}`, known: true },
    prod('Кредиты', p.loans),
    prod('Лизинг', p.leasing),
    prod('Гарантии', p.guarantees),
    prod('Депозиты', p.deposits),
    prod('Факторинг', p.factoring),
    c.fts.flag === null ? { label: 'ФТС: неизвестно', known: false } : { label: c.fts.flag ? `ФТС: есть запись (${c.fts.country})` : 'ФТС: записи нет', known: true },
  ];
}
