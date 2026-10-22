"""Render measured results; does not rerun collection or inference."""
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

HERE=Path(__file__).resolve().parent


def main():
    r=json.loads((HERE/'artifacts/benchmark.json').read_text())
    validation=json.loads((HERE/'artifacts/validation.json').read_text())
    assert validation['status']=='passed' and validation['input_sha256']==r['input_sha256']
    c=r['collection'];s=r['company_search']
    date=datetime.fromisoformat(r['created_at']).astimezone(ZoneInfo('Europe/Moscow')).isoformat(timespec='seconds')
    lines=['# Результат CPU-эксперимента', '', f'Измерено: {date}.', '',
           '## Условия', '',
           f"- CPU: {r['cpu']}; affinity ограничена {r['cpu_limit']} логическими CPU: {r['cpu_affinity']}. Хост имеет {r['original_available_logical_cpus']} доступных логических CPU.",
           f"- Модель: {r['model']}, revision `{r['revision']}`; {r['weight_format']}; вектор 384 float32.",
           f"- Execution provider: {r['provider']}. GPU не использовался.",
           f"- Python {r['python']}; ONNX Runtime {r['versions']['onnxruntime']}; tokenizer {r['versions']['tokenizers']}.",
           f"- 100 уникальных URL/текстов; {r['repeats']} прохода каждого режима. Tokenization, inference, pooling и normalization включены в encode.",
           '- Загрузка модели и warmup исключены из throughput и записаны отдельно. Подготовка индекса компаний также не включена в скорость новостей.', '',
           '## Корпус', '',
           f"Источники: {c['sources_distribution']}. Извлечение: {c['extraction_distribution']}.",
           f"Это 100 настоящих новостных записей: {c['extraction_distribution']['rss_summary']} RSS-анонсов и {c['extraction_distribution']['trafilatura_public_html']} извлечённых текстов страниц. Публикации текущей выборки перечислены с URL в artifacts/articles.jsonl.",
           'Все 100 пакетов прошли B6 v0.2 validation и проверку цитат. Проверен настоящий пакетный импорт в EventStore, повтор дал unchanged для всех 100 записей.',
           '**B6 — quote-only упаковка без LLM.** Неизвестные semantic fields оставлены неизвестными. Этот эксперимент не проверяет качество извлечения фактов, идентичность событий или production-ранжирование.', '',
           '## Скорость', '',
           '| Вход | Лимит токенов | Batch | Среднее мс/текст | Текстов/мин | p95 задержки батча, мс |',
           '|---|---:|---:|---:|---:|---:|']
    names={'event':'Заголовок + lead','article':'Заголовок + доступный текст','longest_article_stress':'Самая длинная статья, повторный stress'}
    for x in r['measurements']:
        lines.append(f"| {names[x['mode']]} | {x['max_tokens']} | {x['batch_size']} | {x['amortized_ms_per_news']:.2f} | {x['throughput_news_per_minute']:.1f} | {x['batch_latency_ms']['p95']:.2f} |")
    lines += ['', 'Для batch=1 p95 относится к одному тексту; для batch=8 — ко всему батчу. Среднее мс/текст для batch=8 является амортизированной стоимостью, а не latency отдельного запроса.',
              'Stress повторяет один самый длинный реальный текст 100 раз за проход, каждый раз вызывая модель без кэша результата. Это проверка нагрузки на длине 512, не дополнительный независимый корпус.', '',
              '## Длины и ограничения', '']
    for mode in ['event','article']:
        t=r['input_tokens'][mode]
        lines.append(f"- {names[mode]}: токены p50={t['distribution']['p50']:.0f}, p95={t['distribution']['p95']:.0f}, max={t['distribution']['max']:.0f}; длиннее 256: {t['over_256']}/100; длиннее 512: {t['over_512']}/100.")
    lines += [f"- Peak RSS процесса: {r['peak_rss_mib']:.1f} MiB; это максимум всего benchmark-процесса, не чистый размер модели и не память всего сервиса.",
              '- Batch padding может ухудшать скорость на смешанных длинах; batch=8 здесь не предполагается автоматически более быстрым.',
              '- INT8 и AVX512 VNNI на Ryzen не эквивалентны fp32 или произвольному облачному CPU. Для выбранного VPS нужен свой замер.', '',
              '## Поиск компаний', '',
              f"- {s['sample_size']} реальных профилей: по одному представителю ОКВЭД, затем выборка с seed=42. Полная база 85 тысяч не векторизована.",
              f"- Подготовка их embeddings: {s['company_embedding_seconds']:.2f} с, один раз отдельно от обработки новостей.",
              f"- Exact cosine top-5 по готовой матрице: p50={s['search_ms']['p50']:.4f} мс, p95={s['search_ms']['p95']:.4f} мс на запрос.",
              '- Примеры сохранены в artifacts/search.examples.json. Ближайшая компания — кандидат по текстовой похожести, не доказанный получатель бизнес-риска. Precision/recall без экспертной разметки не оценены.', '',
              '## Что можно заключить', '',
              ('**На этой машине и этой модели цель 60 CPU-эмбеддингов в минуту достигнута во всех режимах, включая длинный stress-прогон.**' if all(x['throughput_news_per_minute']>=60 for x in r['measurements']) else '**Не все режимы достигли 60 CPU-эмбеддингов в минуту; смотрите таблицу.**') + ' Это не измерение 60 полных персонализированных инсайтов в минуту: LLM, RAG, сбор новостей, QA и массовая дооценка пар не включены.',
              'Корпус короткий и преимущественно состоит из RSS-анонсов; по нему нельзя обещать такую же скорость на произвольно длинных статьях или другой модели. Поиск показан на 256 компаниях, его результат нельзя выдавать за проверенную персонализацию всей базы.', '',
              '## Артефакты и воспроизведение', '',
              'Код и команды: README.md. Полные измерения: artifacts/benchmark.json. Корпус B6: artifacts/events.v0.2.json и .jsonl. Векторы: artifacts/news.embeddings.npy. Модель и исходные данные игнорируются Git.',
              'CPU/API cost: платные LLM не использовались, локальная embedding-модель работает без API-ключа. Время загрузки модели не включено в steady-state скорость.']
    (HERE/'RESULTS.md').write_text('\n'.join(lines)+'\n')
    print(HERE/'RESULTS.md')


if __name__=='__main__': main()
