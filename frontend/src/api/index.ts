import type { CompanyProfile, DemoSettings, FeedItem, Insight, NewsEvent, QaAnswer, Role } from '../types';
import * as mock from './mock/engine';

/* Единая точка доступа UI к данным. Режим выбирается VITE_API_MODE=mock|http.
   Контракт HTTP-режима — docs/frontend-api-contract.md; пути можно поменять только здесь. */

/* Настройки читаем сначала из public/config.js (в Docker его подставляет контейнер при старте),
   потом из VITE_* на этапе сборки. Пустой apiUrl означает «тот же адрес, откуда открыт сайт». */
declare global {
  interface Window { __APP_CONFIG__?: { apiMode?: string; apiUrl?: string } }
}
const cfg = window.__APP_CONFIG__ ?? {};
const MODE = cfg.apiMode || (import.meta.env.VITE_API_MODE as string | undefined) || 'mock';
const BASE = (cfg.apiUrl ?? (import.meta.env.VITE_API_URL as string | undefined) ?? '').replace(/\/$/, '');

async function http<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { headers: { 'Content-Type': 'application/json' }, ...init });
  if (!res.ok) throw new Error(`Ошибка сервиса (${res.status})`);
  const body = await res.text();
  return (body ? JSON.parse(body) : undefined) as T;
}

export interface Api {
  mode: 'mock' | 'http';
  getCompanies(): Promise<CompanyProfile[]>;
  getFeed(companyId: string, role: Role): Promise<FeedItem[]>;
  getInsight(eventId: string, companyId: string, role: Role): Promise<Insight>;
  ask(insight: Insight, question: string): Promise<QaAnswer>;
  ingestNews(title: string, text: string): Promise<NewsEvent>;
  sendFeedback(runId: string, useful: boolean): Promise<void>;
}

/* Пути и формы ответов описаны в docs/frontend-api-contract.md. Префикс /api/v1 совпадает с backend/main.py. */
const q = encodeURIComponent;

const httpApi: Api = {
  mode: 'http',
  getCompanies: () => http('/api/v1/companies'),
  getFeed: (companyId, role) => http(`/api/v1/companies/${q(companyId)}/news-feed?role=${role}`),
  getInsight: (eventId, companyId, role) => http(`/api/v1/news/${q(eventId)}/analysis?company_id=${q(companyId)}&role=${role}`),
  ask: (insight, question) =>
    http('/api/v1/chat/ask', {
      method: 'POST',
      body: JSON.stringify({ company_id: insight.company_id, news_id: insight.event.event_id, role: insight.user_role, run_id: insight.run_id, question }),
    }),
  ingestNews: (title, text) => http('/api/v1/news', { method: 'POST', body: JSON.stringify({ title, text }) }),
  sendFeedback: async (runId, useful) => {
    await http('/api/v1/feedback', { method: 'POST', body: JSON.stringify({ run_id: runId, useful }) });
  },
};

const mockApi: Api = {
  mode: 'mock',
  getCompanies: mock.getCompanies,
  getFeed: mock.getFeed,
  getInsight: mock.getInsight,
  ask: mock.ask,
  ingestNews: mock.ingestNews,
  sendFeedback: mock.sendFeedback,
};

export const api: Api = MODE === 'http' ? httpApi : mockApi;

/** Демо-переключатели (задержка, ошибка, исчерпанный бюджет) работают только в mock-режиме. */
export const demoSettings: DemoSettings = mock.settings;
export const resetDemoNews = mock.resetUserEvents;
/** Имитация «пришло новое событие из источников» (демо, только mock). */
export const simulateIncoming = mock.simulateIncoming;
export const DEMO_TODAY = mock.DEMO_TODAY;
