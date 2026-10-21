import { useState, type FormEvent } from 'react';
import { href, navigate } from '../router';
import { DEMO_LOGIN, useApp } from '../store';
import { ArtChart } from '../components/Illustrations';
import { Button } from '../components/ui';

/** Вход в демо. В банке логин и пароль принадлежат физлицу, а доступ к компаниям и группам определяется его правами;
    здесь проверка условная, данные для входа показаны на экране, чтобы эксперты могли пройти сценарий. */
export function LoginPage() {
  const { login } = useApp();
  const [name, setName] = useState('');
  const [pass, setPass] = useState('');
  const [error, setError] = useState(false);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (login(name, pass)) navigate(href.select);
    else setError(true);
  };

  return (
    <div className="select-page">
      <div className="select-grid">
        <header className="select-hero">
          <span className="brand__mark brand__mark--lg" aria-hidden />
          <h1 className="select-title">Новости, которые касаются именно вас</h1>
          <p className="select-lead">Войдите, чтобы увидеть события, значимые для ваших компаний, и понять почему.</p>
          <p className="small select-note">Демо-вход. Компании и данные синтетические, настоящий интернет-банк не затрагивается.</p>
          <div className="select-hero__art" aria-hidden><ArtChart /></div>
        </header>

        <form className="select-form login-form" onSubmit={submit} noValidate>
          <h2 className="sec-title">Вход в Альфа-Бизнес</h2>
          <div className="field">
            <label htmlFor="login-name">Логин</label>
            <input id="login-name" autoComplete="username" value={name} onChange={(e) => { setName(e.target.value); setError(false); }} aria-invalid={error} />
          </div>
          <div className="field">
            <label htmlFor="login-pass">Пароль</label>
            <input id="login-pass" type="password" autoComplete="current-password" value={pass} onChange={(e) => { setPass(e.target.value); setError(false); }} aria-invalid={error} aria-describedby={error ? 'login-err' : undefined} />
          </div>
          {error && <p id="login-err" className="field-error" role="alert">Неверный логин или пароль. Данные для демо указаны ниже.</p>}
          <Button type="submit" variant="primary" size="xl" block disabled={!name || !pass}>Войти</Button>

          <section className="login-hint" aria-label="Данные для демо-входа">
            <p className="medium">Данные для демо-входа</p>
            <dl className="small">
              <div><dt>Логин</dt><dd><code>{DEMO_LOGIN.login}</code></dd></div>
              <div><dt>Пароль</dt><dd><code>{DEMO_LOGIN.password}</code></dd></div>
              <div><dt>Пользователь</dt><dd>{DEMO_LOGIN.name}</dd></div>
            </dl>
            <p className="small muted">После входа вы выберете компанию из тех, к которым у пользователя есть доступ, и роль.</p>
            <Button size="s" onClick={() => { setName(DEMO_LOGIN.login); setPass(DEMO_LOGIN.password); setError(false); }}>Подставить</Button>
          </section>
        </form>
      </div>
    </div>
  );
}
