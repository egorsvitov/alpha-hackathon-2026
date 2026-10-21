import { Component, type ErrorInfo, type ReactNode } from 'react';

/** Ловит ошибки отрисовки. Самая частая причина: backend ответил не в том формате, что описан в контракте. */
export class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Ошибка отрисовки', error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <main className="fatal" role="alert">
        <h1 className="page-title">Не удалось показать данные</h1>
        <p className="muted">
          Скорее всего, ответ сервера не подходит интерфейсу. Формат ответов описан в docs/frontend-api-contract.md.
          Чтобы посмотреть демо без backend, запустите фронт в режиме mock (APP_API_MODE=mock).
        </p>
        <p className="small muted"><code>{this.state.error.message}</code></p>
        <button type="button" className="btn btn--primary btn--l" onClick={() => window.location.reload()}>Обновить страницу</button>
      </main>
    );
  }
}
