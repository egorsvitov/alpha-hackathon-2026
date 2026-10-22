"""Validate cross-model artifacts and render the measured comparison in Russian."""
import hashlib
import json
from pathlib import Path
import numpy as np

HERE=Path(__file__).resolve().parent
ART=HERE/'artifacts'

def main():
    reports=json.loads((ART/'dimensions-comparison.json').read_text())
    assert {r['dimension'] for r in reports}=={384,768,1024}
    corpus_hash=hashlib.sha256((ART/'articles.jsonl').read_bytes()).hexdigest()
    reference_companies=None
    rankings={}
    checks=[]
    warnings=[]
    for r in reports:
        d=r['dimension'];p=ART/f'comparison-{d}'
        for mode, consistency in r['batch_consistency'].items():
            if consistency['min']<=0.95:
                warnings.append({'dimension':d,'mode':mode,'min_cosine':consistency['min'],'threshold':0.95})
        assert r['input_sha256']==corpus_hash
        assert r['provider']=='CPUExecutionProvider' and len(r['cpu_affinity'])==2
        assert r['repeats']==3 and len(r['measurements'])==6
        companies=json.loads((p/'companies.sample.json').read_text())
        if reference_companies is None: reference_companies=companies
        assert companies==reference_companies
        news=np.load(p/'news.embeddings.npy');profiles=np.load(p/'companies.sample.embeddings.npy')
        for matrix,rows in [(news,100),(profiles,256)]:
            assert matrix.shape==(rows,d) and matrix.dtype==np.float32
            assert np.isfinite(matrix).all()
            assert np.allclose(np.linalg.norm(matrix,axis=1),1,atol=1e-5)
        assert np.allclose(np.diag(news@news.T),1,atol=1e-5)
        rankings[d]=np.argsort(-(news@profiles.T),axis=1)[:,:5]
        checks.append({'dimension':d,'status':'passed','input_sha256':corpus_hash,
                       'news_shape':list(news.shape),'company_shape':list(profiles.shape),
                       'news_nbytes':news.nbytes,'company_nbytes':profiles.nbytes})
    agreements={str(d):float(np.mean([len(set(a)&set(b))/5 for a,b in zip(rankings[384],rankings[d])]))
                for d in (768,1024)}
    (ART/'dimensions-validation.json').write_text(json.dumps({'status':'passed_with_warnings' if warnings else 'passed','checks':checks,'batch_consistency_warnings':warnings,
        'top5_overlap_with_384':agreements,'quality_evaluated':False},ensure_ascii=False,indent=2)+'\n')
    lines=['# CPU-эмбеддинги: сравнение 384, 768 и 1024', '',
        f"Замеры: {reports[0]['created_at']}. CPU: {reports[0]['cpu']}.",
        'Каждый процесс ограничен двумя одинаковыми логическими CPU; ONNX Runtime CPUExecutionProvider, INT8 AVX512 VNNI. GPU и платные API не используются.', '',
        '## Данные и методика', '',
        'Один сохранённый корпус: 100 уникальных реальных новостей (25 текстов публичных HTML-страниц и 75 RSS-анонсов), те же 256 компаний. Три прохода для каждой комбинации модели, режима текста и batch size. Порядок запуска: small → base → large. Это последовательные локальные замеры без контроля температуры/частот, а не облачный SLA.',
        f'Корпус SHA256: `{corpus_hash}`.',
        'Title+lead ограничен 256 токенами, title+доступный текст — 512. Stress — одна самая длинная статья, повторённая 100 раз без кэша, с ограничением 512 токенов. Это не дополнительные независимые новости.',
        'Время включает токенизацию, inference, mean pooling и L2-нормализацию; исключает сбор, скачивание, загрузку модели, warmup, подготовку компаний, LLM и QA.', '',
        '## Скорость одиночной обработки', '',
        '| Модель / размер | Title+lead, текстов/мин | Доступный текст, текстов/мин | Stress до 512, текстов/мин | p95 доступного текста, мс |',
        '|---|---:|---:|---:|---:|']
    for r in reports:
        m={v['mode']:v for v in r['measurements'] if v['batch_size']==1}
        lines.append(f"| {r['model'].split('/')[-1]} / {r['dimension']} | {m['event']['throughput_news_per_minute']:.0f} | {m['article']['throughput_news_per_minute']:.0f} | {m['longest_article_stress']['throughput_news_per_minute']:.0f} | {m['article']['batch_latency_ms']['p95']:.1f} |")
    lines += ['', '## Пакетная обработка (batch=8)', '',
        '| Размер | Title+lead, текстов/мин | Доступный текст, текстов/мин | Stress, текстов/мин | p95 батча доступного текста, мс |',
        '|---|---:|---:|---:|---:|']
    for r in reports:
        m={v['mode']:v for v in r['measurements'] if v['batch_size']==8}
        lines.append(f"| {r['dimension']} | {m['event']['throughput_news_per_minute']:.0f} | {m['article']['throughput_news_per_minute']:.0f} | {m['longest_article_stress']['throughput_news_per_minute']:.0f} | {m['article']['batch_latency_ms']['p95']:.1f} |")
    lines += ['', 'p95 батча — задержка всего пакета, не одного пользователя. Batch=8 не гарантирует ускорения; разная длина текстов приводит к padding.', '',
        '## Память, веса и поиск компаний', '',
        '| Размер | Веса ONNX, МиБ | Peak RSS процесса, МиБ | Один вектор float32, байт | 85 514 векторов, МиБ | Подготовка 256 компаний, с | Поиск top-5 по 256, p95 мс |',
        '|---|---:|---:|---:|---:|---:|---:|']
    for r in reports:
        d=r['dimension'];search=r['company_search'];weights=r['model_files']['onnx/model_qint8_avx512_vnni.onnx']['bytes']
        lines.append(f"| {d} | {weights/2**20:.1f} | {r['peak_rss_mib']:.1f} | {d*4} | {85514*d*4/2**20:.1f} | {search['company_embedding_seconds']:.2f} | {search['search_ms']['p95']:.4f} |")
    lines += ['', 'Для примеров поиска новости берутся из batch=1, а компании заранее кодируются batch=8, как в исходном эксперименте. При обнаруженном batch drift это также ограничивает интерпретацию выдачи.',
        'Peak RSS — максимум всего процесса за запуск (включая tokenizer, временные буферы и последовательные сессии), не минимальные требования сервиса. Число для 85 514 компаний — расчёт размера матрицы float32; полный индекс не строился, его накладные расходы не включены. Поиск измерен только по 256 компаниям.', '',
        '## Обрезание и проверки', '', '| Размер | Токены доступного текста p50 / p95 / max | Обрезано свыше 512 | Минимальное cosine batch1 vs batch8 |', '|---|---:|---:|---:|']
    for r in reports:
        dist=r['input_tokens']['article']['distribution']
        minimum=min(x['min'] for x in r['batch_consistency'].values())
        lines.append(f"| {r['dimension']} | {dist['p50']:.0f} / {dist['p95']:.0f} / {dist['max']:.0f} | {r['input_tokens']['article']['over_512']} / 100 | {minimum:.5f} |")
    if warnings:
        lines += ['', '**Предупреждение: INT8 зависит от состава батча.** Для перечисленных режимов minimum cosine между batch=1 и batch=8 не превысил проверочный порог 0.95: '+', '.join(f"{w['dimension']} / {w['mode']}: {w['min_cosine']:.5f}" for w in warnings)+'. Значения скорости сохраняются, но batch=8 нельзя считать взаимозаменяемым с batch=1. Для использования нужны отдельная проверка FP32-эталоном/разметкой либо фиксированный одиночный режим. Согласованность с FP32 в этом эксперименте не проверяется.']
    lines += ['', 'Проверены размерности 100×D и 256×D, float32, конечность чисел, единичная норма, self-cosine, идентичность корпуса и профилей. Эти проверки пройдены. Согласованность batch1/batch8 измерена отдельно: значения ≤0.95 считаются предупреждением, а не успешной проверкой качества. Порог 0.95 — эвристическая диагностика численной устойчивости, не метрика точности рекомендаций. B6 не преобразовывался повторно: использована прежняя quote-only упаковка, не LLM-извлечение фактов.', '',
        '## Что это значит для проекта', '']
    worst=min(v['throughput_news_per_minute'] for r in reports for v in r['measurements'])
    lines += [f'Минимальная измеренная скорость среди всех режимов — {worst:.0f} текстов/мин. '+('Все комбинации превысили ориентир 60 текстов/мин на этом CPU.' if worst>=60 else 'Не все комбинации достигли ориентира 60 текстов/мин.'),
        'Увеличение размерности здесь означает замену модели: base имеет 12 слоёв, large — 24. Результат нельзя объяснить только количеством чисел в векторе. Размер входа у всех ограничен 512 токенами.',
        'Векторы разных моделей нельзя смешивать в одном индексе. При замене модели нужно заново векторизовать и новости, и компании; стоимость подготовки базы оплачивается один раз на версию профиля/модели.',
        f"Среднее пересечение top-5 с вариантом 384: для 768 — {agreements['768']:.1%}, для 1024 — {agreements['1024']:.1%}. Это только согласованность выдач, не точность и не Recall@5.",
        '**Качество рекомендаций не измерено:** нет экспертных меток, а более длинный вектор сам по себе не доказывает лучшее понимание бизнес-влияния. Для выбора модели нужен размеченный набор «новость — компания», затем Recall@K для отбора кандидатов и nDCG@K для ранжирования.',
        'Сохранённые примеры ближайших компаний позволяют провести такой обзор вручную. Production-ранкер этим экспериментом не изменён.', '',
        '## Воспроизведение и артефакты', '',
        '```bash',
        'uv run --project ranking/experiments/cpu_news_embeddings --frozen python ranking/experiments/cpu_news_embeddings/compare_dimensions.py',
        'uv run --project ranking/experiments/cpu_news_embeddings --frozen python ranking/experiments/cpu_news_embeddings/report_dimensions.py',
        '```',
        'Нужен прежний сохранённый корпус `artifacts/articles.jsonl` и `events.v0.2.jsonl`. Повторный collect изменит корпус. Веса скачиваются один раз из зафиксированных revisions.',
        '`artifacts/comparison-{384,768,1024}/` содержит benchmark.json, матрицы новостей/компаний и примеры поиска. `dimensions-comparison.json` объединяет замеры, `dimensions-validation.json` фиксирует проверки. Веса, тексты и реальные профили исключены из Git.', '',
        '## Первичные источники моделей', '',
        '[E5-small](https://huggingface.co/intfloat/multilingual-e5-small), [E5-base](https://huggingface.co/intfloat/multilingual-e5-base), [E5-large](https://huggingface.co/intfloat/multilingual-e5-large). Точные revisions и SHA256 файлов сохранены в каждом benchmark.json и model*/manifest.json. Все три модели распространяются по MIT; лицензия не переносится на тексты новостей.']
    (HERE/'DIMENSIONS_RESULTS.md').write_text('\n'.join(lines)+'\n')
    print(f'Structural checks passed; batch-consistency warnings: {len(warnings)}; DIMENSIONS_RESULTS.md written')

if __name__=='__main__': main()
