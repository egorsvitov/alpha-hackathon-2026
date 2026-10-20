import type { Topic } from '../../types';
import type { Driver } from '../../data/events';

// Классификатор тем для НОВЫХ новостей (загрузка пользователем). Ключевые слова + шаблоны влияния.
// Это заглушка вместо LLM/классификатора: контракт ответа тот же, что у остальных событий.

export interface TopicTemplate {
  topic: Topic;
  keywords: RegExp;
  drivers: Driver[];
  bank_ids: string[];
  /** первый запрос в поиске аналитики банка (может ничего не найти → повторный поиск) */
  first_query: string;
  retry_query: string;
  generic: { risks: string[]; opportunities: string[]; actions: string[] };
}

export const TOPIC_TEMPLATES: TopicTemplate[] = [
  {
    topic: 'rates',
    keywords: /ставк|кредит|рефинанс|ипотек|займ/i,
    drivers: [
      { feature: 'product.loans', weight: 0.3, phrase: 'ваши кредитные продукты', mechanism: 'изменение условий кредитования влияет на стоимость обслуживания долга' },
      { feature: 'product.leasing', weight: 0.1, phrase: 'лизинг', mechanism: 'лизинговые платежи зависят от стоимости фондирования' },
      { feature: 'product.deposits', weight: 0.15, phrase: 'ваши депозиты', mechanism: 'изменение ставок влияет на доходность размещений' },
    ],
    bank_ids: ['b_key'],
    first_query: 'условия кредитования новость',
    retry_query: 'ключевая ставка ЦБ РФ',
    generic: {
      risks: ['Рост стоимости заёмных средств при плавающих ставках.'],
      opportunities: ['Пересмотреть структуру долга и размещений с учётом нового уровня ставок.'],
      actions: ['Сверить структуру кредитов, лизинга и депозитов с новыми условиями.'],
    },
  },
  {
    topic: 'fx',
    keywords: /курс|рубл|валют|юан|доллар|евро/i,
    drivers: [
      { feature: 'fts', weight: 0.3, phrase: 'внешнеторговые расчёты', mechanism: 'курс влияет на рублёвую стоимость валютной выручки и платежей' },
      { feature: 'industry:grain', weight: 0.12, phrase: 'экспорт зерна', mechanism: 'цены на зерно номинированы в валюте' },
    ],
    bank_ids: ['b_tact'],
    first_query: 'курс рубля прогноз',
    retry_query: 'тактический взгляд валютный риск',
    generic: {
      risks: ['Изменение рублёвой стоимости валютных обязательств и платежей.'],
      opportunities: ['Зафиксировать выгодный курс для части валютной выручки или платежей.'],
      actions: ['Проверить открытую валютную позицию и график валютных платежей.'],
    },
  },
  {
    topic: 'sanctions',
    keywords: /санкц|квот|запрет|ограничен|эмбарго/i,
    drivers: [
      { feature: 'fts', weight: 0.2, phrase: 'внешнеторговая деятельность', mechanism: 'ограничения могут затрагивать экспортные и импортные операции' },
      { feature: 'industry:metallurgy', weight: 0.25, phrase: 'ваша отрасль — металлургия', mechanism: 'ограничения часто касаются металлопродукции' },
    ],
    bank_ids: ['b_comm'],
    first_query: 'санкции квоты экспорт',
    retry_query: 'commodities торговые ограничения экспортёры',
    generic: {
      risks: ['Потеря части экспортных рынков и переориентация поставок.'],
      opportunities: ['Диверсификация рынков сбыта с торговым финансированием.'],
      actions: ['Оценить долю выручки, зависящую от затронутых рынков.'],
    },
  },
  {
    topic: 'trade',
    keywords: /пошлин|экспорт|зерн|пшениц|урожай|агро/i,
    drivers: [
      { feature: 'industry:grain', weight: 0.4, phrase: 'ваша отрасль — экспорт зерна', mechanism: 'меры по торговле зерном влияют на экспортную маржу' },
      { feature: 'product.factoring', weight: 0.08, phrase: 'факторинг', mechanism: 'изменение оборотного капитала увеличивает спрос на финансирование поставок' },
    ],
    bank_ids: ['b_comm'],
    first_query: 'экспортная пошлина зерно',
    retry_query: 'commodities performance сельхозсырьё',
    generic: {
      risks: ['Снижение маржи экспорта и рост потребности в оборотном капитале.'],
      opportunities: ['Финансирование поставок факторингом, переориентация объёмов.'],
      actions: ['Пересчитать маржинальность действующих контрактов.'],
    },
  },
  {
    topic: 'industry',
    keywords: /сталь|металл|прокат|уголь|кокс|чугун|руда/i,
    drivers: [
      { feature: 'industry:metallurgy', weight: 0.4, phrase: 'ваша отрасль — металлургия', mechanism: 'событие касается сырья или продукции отрасли' },
      { feature: 'counterparty:coke_resource', weight: 0.15, phrase: 'ваш поставщик', mechanism: 'событие может затрагивать цепочку поставок сырья' },
    ],
    bank_ids: ['b_comm'],
    first_query: 'металлургия сталь прокат цены',
    retry_query: 'commodities performance металлы',
    generic: {
      risks: ['Изменение цен на сырьё или готовую продукцию и перебои в цепочке поставок.'],
      opportunities: ['Пересмотр контрактов и диверсификация поставщиков.'],
      actions: ['Оценить запасы сырья и альтернативных поставщиков.'],
    },
  },
  {
    topic: 'inflation',
    keywords: /инфляц|цен[ыау]|продукт|потреблен|спрос|розниц/i,
    drivers: [
      { feature: 'industry:retail_food', weight: 0.3, phrase: 'ваша отрасль — продуктовая розница', mechanism: 'рост цен влияет на выручку, закупки и спрос' },
      { feature: 'product.deposits', weight: 0.06, phrase: 'ваши депозиты', mechanism: 'устойчивая инфляция сохраняет жёсткость денежной политики' },
    ],
    bank_ids: ['b_infl', 'b_cons'],
    first_query: 'потребительские цены',
    retry_query: 'инфляция основные выводы',
    generic: {
      risks: ['Рост закупочных цен и сжатие маржи при ограничении спроса.'],
      opportunities: ['Долгосрочные контракты с поставщиками и размещение выручки.'],
      actions: ['Обновить прогноз маржи и оборотного капитала.'],
    },
  },
];

export const OTHER_TEMPLATE: TopicTemplate = {
  topic: 'other',
  keywords: /./,
  drivers: [],
  bank_ids: [],
  first_query: 'общий поиск по теме новости',
  retry_query: 'расширенный поиск по ключевым словам',
  generic: { risks: [], opportunities: [], actions: [] },
};

export function classify(text: string): TopicTemplate {
  return TOPIC_TEMPLATES.find((t) => t.keywords.test(text)) ?? OTHER_TEMPLATE;
}
