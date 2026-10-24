import { useState, type FormEvent } from 'react';
import { href, navigate } from '../router';
import { DEMO_LOGIN, useActions } from '../store';
import { ArtLogin } from '../components/Illustrations';
import { BrandLockup } from '../components/Brand';

/** Вход в демо. Компоновка повторяет страницу входа в интернет-банк: белая страница, одна колонка, крупный заголовок,
    серые поля, тёмная кнопка, серая карточка с картинкой. В банке логин и пароль принадлежат физлицу, а доступ к компаниям
    определяется его правами; здесь проверка условная, данные для входа показаны на экране, чтобы эксперты могли пройти сценарий. */
export function LoginPage() {
  const { login } = useActions();
  const [name, setName] = useState('');
  const [pass, setPass] = useState('');
  const [error, setError] = useState(false);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (login(name, pass)) navigate(href.select);
    else setError(true);
  };
  const fill = () => { setName(DEMO_LOGIN.login); setPass(DEMO_LOGIN.password); setError(false); };

  return (
    <div className="login">
      <header className="login__top">
        <span className="brand">
          <BrandLockup size={40} />
        </span>
        <span className="login__demo">Демо-режим</span>
      </header>

      <div className="login__col">
        <h1 className="login__title">Привет! Войдите<br />в интернет-банк</h1>

        <form className="login__form" onSubmit={submit} noValidate>
          <div className="login-field" data-invalid={error}>
            <label htmlFor="login-name">Логин</label>
            <input id="login-name" autoComplete="username" value={name} onChange={(e) => { setName(e.target.value); setError(false); }} aria-invalid={error} />
          </div>
          <div className="login-field" data-invalid={error}>
            <label htmlFor="login-pass">Пароль</label>
            <input id="login-pass" type="password" autoComplete="current-password" value={pass} onChange={(e) => { setPass(e.target.value); setError(false); }} aria-invalid={error} aria-describedby={error ? 'login-err' : undefined} />
          </div>
          {error && <p id="login-err" className="field-error" role="alert">Неверный логин или пароль. Данные для демо указаны ниже.</p>}
          <button type="submit" className="login__submit" disabled={!name || !pass}>Войти</button>
          <button type="button" className="link-btn login__alt" onClick={fill}>Подставить данные для демо</button>
        </form>

        <section className="login-card" aria-labelledby="demo-creds">
          <div className="login-card__art" aria-hidden><ArtLogin /></div>
          <div>
            <h2 id="demo-creds">Демо-вход</h2>
            <dl>
              <div><dt>Логин</dt><dd><code>{DEMO_LOGIN.login}</code></dd></div>
              <div><dt>Пароль</dt><dd><code>{DEMO_LOGIN.password}</code></dd></div>
              <div><dt>Пользователь</dt><dd>{DEMO_LOGIN.name}</dd></div>
            </dl>
          </div>
        </section>

        <p className="small muted login__note">
          После входа вы выберете компанию из тех, к которым у пользователя есть доступ, и роль. Компании и данные синтетические, настоящий интернет-банк не затрагивается.
        </p>
      </div>
    </div>
  );
}
