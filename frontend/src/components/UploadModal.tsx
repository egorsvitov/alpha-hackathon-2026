import { useState } from 'react';
import { Upload } from 'lucide-react';
import { api } from '../api';
import { useActions } from '../store';
import { Button, Modal } from './ui';

const SAMPLES = [
  {
    label: 'Пример: НДС для экспортёров',
    title: 'Правительство изменило порядок возмещения НДС для экспортёров',
    text: 'Правительство утвердило новый порядок возмещения НДС для экспортёров. Срок возврата налога может вырасти. Изменение касается экспортных поставок с начала следующего года.',
  },
  {
    label: 'Пример: цены на уголь',
    title: 'Цены на коксующийся уголь выросли на фоне перебоев поставок',
    text: 'Стоимость коксующегося угля выросла за неделю. Металлурги отмечают сокращение запасов сырья на складах и рост издержек.',
  },
  {
    label: 'Пример: непонятная новость',
    title: 'Совет директоров назначил нового корпоративного секретаря',
    text: 'Совет директоров крупной организации назначил нового корпоративного секретаря сроком на три года.',
  },
];

/** Загрузка новой тестовой новости: идёт через тот же алгоритм, что и остальные (критерий 05). */
export function UploadModal({ onClose, onDone }: { onClose(): void; onDone(eventId: string): void }) {
  const { bumpFeed } = useActions();
  const [title, setTitle] = useState('');
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const ev = await api.ingestNews(title.trim(), text.trim());
      bumpFeed();
      onDone(ev.event_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Не удалось обработать новость');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title="Загрузить новость"
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" size="l" onClick={onClose}>Отмена</Button>
          <Button variant="primary" size="l" loading={busy} disabled={title.trim().length < 5} onClick={() => void submit()} icon={<Upload size={18} aria-hidden />}>Обработать</Button>
        </>
      }
    >
      <p className="muted">Новость пройдёт тот же путь, что и остальные: определение темы, поиск аналитики банка (с повторным поиском при неполноте), ранжирование под выбранную компанию.</p>
      <div className="field">
        <label htmlFor="up-title">Заголовок</label>
        <input id="up-title" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Например: Банк России изменил требования к…" />
      </div>
      <div className="field">
        <label htmlFor="up-text">Текст новости</label>
        <textarea id="up-text" rows={5} value={text} onChange={(e) => setText(e.target.value)} placeholder="Вставьте текст (необязательно)" />
      </div>
      <div className="qa-chips">
        {SAMPLES.map((s) => (
          <button type="button" className="suggest" key={s.label} onClick={() => { setTitle(s.title); setText(s.text); }}>{s.label}</button>
        ))}
      </div>
      {error && <p className="field-error" role="alert">{error}</p>}
    </Modal>
  );
}
