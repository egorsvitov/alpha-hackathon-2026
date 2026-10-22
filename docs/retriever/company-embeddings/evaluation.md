# Протокол проверки эмбеддингов и графа

Расширение R0/R6 из [общего плана](../implementation-plan.md). Не новый источник ground truth. Все результаты качества пока неизвестны; ниже — заранее фиксируемая процедура.

## 1. Dataset и разметка

Начальный R0: 8 компаний × 100 уникальных событий, CFO, полная разметка 800 пар. Профили: автомобильные и авиационные перевозки, IT, торговля с/без ВЭД, строительство, производство, неполный профиль. Если реальной записи с нужными признаками нет, synthetic case хранится отдельно и не выдаётся за распределение реальных компаний.

Графовые strata дополнительно выбираются по обоим направлениям и сохраняются ДО результатов retrieval:

- good: хотя бы одно направление имеет >=3 business-backed neighbors и vector_amount_fraction>=0.8;
- weak: есть хотя бы один business vector, но good не выполнен;
- no_graph: ни одного business-backed соседа в обоих направлениях (отдельно distinguish no_edges vs unknown_only).

Это категории эксперимента, не threshold eligibility runtime. Низкое coverage не обнуляет автоматом graph view; эффект измеряется отдельно. При выборе 8 профилей постараться иметь >=2 на stratum; если секторные/graph требования не совмещаются, явно расширить pilot до 12–24 реальных профилей, не подменять coverage. Конечные IDs и seed заморозить в dataset manifest.

Имеющийся корпус 100 реальных новостей — 25 HTML-текстов и 75 RSS-анонсов; B6 quote-only, не полноценное LLM извлечение. Сохранить source types. Для утверждения о широком качестве расширить корпус до нескольких дат/историй. Домодедово и B6 negation/scenario/conflict examples — отдельные regression cases; не переименовывать тестовые сценарии в real news.

Judgment fields: company_id, company_context_hash, role, event_id/version/payload_hash, story_group_id, label=0|1|2|3|null, status=judged|unknown|not_judged, label_source=human|weak|synthetic_test, annotator, annotation_version, basis_feature_refs, basis_claim_refs, assumptions, synthetic flag. Для null нет числовой метки. Relevant=label>=2; class3 отдельно. Weak никогда автоматически не becomes human.

Все варианты оцениваются относительно одного полного доступного контекста (включая graph evidence и unknowns), а не каждому варианту собственная «истина». Дать аннотатору факты без вариантов выдачи/score. Платёжный сосед из отрасли не делает все новости отрасли релевантными. Для 2 нужны объяснимые условия возможного влияния, для 3 — подтверждённые предпосылки существенности. Часть 2/3 и спорные 0/1 независимо проверяют два человека; disagreement сохраняется и разрешается явно.

Split по news story groups/времени до tuning; перепечатки/версии одной истории не попадают в dev и test одновременно. Каждый тестовый retrieval universe имеет свои eligible events и полностью размеченные пары, иначе denominator нельзя назвать полным recall. На малом корпусе обеспечить >=20 событий в test universe; сообщать его реальный размер и долю positives. Проверка held-out companies полезна на расширенном наборе; 8 профилей недостаточно для уверенной генерализации. Dataset, split и labels имеют hashes.

## 2. Временная применимость

События всегда соблюдают B6 availability/receipt/publication/as_of. Полный платёжный агрегат заявлен до 31.10.2026; available_at неизвестен. Нельзя присвоить ему fake available_at=дата новости.

Два режима:

- strict: graph включается только с проверенным available_at и допустимым period_end. При отсутствии — explicit omitted; такой набор может не позволить оценить граф вообще.
- static_context diagnostic: явно исследуется связь фиксированной таблицы компаний/платежей с фиксированным новостным корпусом, без утверждения о historical performance. Одинаковый context для всех вариантов. Future B6 события всё равно запрещены. Отчёт и run manifests содержат этот флаг; это не основание для производственного использования будущего графа.

При unknown profile data_as_of это также отмечается в limitations для base variants. Нельзя снять метку статического эксперимента просто выбором большого as_of без подтверждённого available_at данных.

## 3. Обязательная матрица

| ID | Каналы и представления | Что сравниваем |
|---|---|---|
| D1 | Dense universal | Один общий вектор |
| D3 | Dense base3 | Тематическое разделение при том же encoder |
| H1 | BM25 + structured + dense universal | Universal в hybrid |
| H3 | BM25 + structured + dense base3 | Базовый hybrid без графа, основной reference |
| H3-I | H3 + graph_incoming в dense views | Эффект отправителей |
| H3-O | H3 + graph_outgoing в dense views | Эффект получателей |
| H3-IO | H3 + оба graph views | Эффект разделения направлений |

Для H3-I/O/IO сначала equal, amount_share, sqrt_share на dev. На test один выбранный policy, либо заранее preregistered полный набор без выбора победителя по test. Одинаковое распределение известных/неизвестных соседей во всех policy.

Дополнительные отдельно маркированные controls:

- H3-C: один объединённый graph вектор, normalize(in+out), с правилом отсутствия направления из contracts;
- H3-S: без graph vectors, structured match по отраслевым агрегатам; этот новый input не попадает в обязательные H3-I/O/IO;
- H3-T: два шаблонных text graph queries вместо центроидов, без LLM; начальный шаблон описывает top5 ОКВЭД по observed amount share, coverage и остальные доли в metadata. Не менять одновременно веса/векторную модель;
- pure BM25 как простой baseline;
- hub penalty — отдельная абляция после выбора simple policy, без hard удаления всех хабов.

## 4. Что фиксируется для сопоставимости

Model/revision/backend/fingerprint, dtype384, prefixes, template versions, eligibility/chunking, corpus/profile/graph snapshot, as_of/window, role, story split, budget counter и параметры fusion.

BM25 и обычные structured inputs во всех mandatory H-вариантах одинаковы. Сравнение H1 vs H3 меняет только dense query representation; BM25 получает один и тот же набор base texts. Граф добавляет dense views, а не четвёртый полноценный канал.

Точный fusion из общей архитектуры:

`D(e)=max_view [1/(60+rank_view(e))]`, отсутствующий rank даёт 0.

`F(e)=D(e)+1/(60+rank_BM25(e))+1/(60+rank_structured(e))`, отсутствующие слагаемые 0. Не суммировать пять отдельных dense contributions и не повышать dense channel weight. Tie-break event_id. R5 реализует формулу один раз; C не пишет её копию в evaluator.

top50 per view/channel → union cap250 по F → pool cap100 → max20 distinct eligible events и token budget8192. Это одинаковые caps; graph делает больше dot products, их время учитывается. Reserve выключен в первой серии; если нужен, отдельная фиксированная для всех policy, без нового резерва «по каждому графовому view».

Универсальный company context в budget одинакового содержания для всех вариантов: графовая доказательная база доступна аналитику независимо от того, участвовала ли она в retrieval. Технические score/матрицы и полный debug trace не включаются в LLM projection. Нельзя выгадать Recall, выдавая расширенному варианту больше места под события.

Пока tokenizer аналитика не выбран, R5 использует единый явно маркированный proxy counter (версия `utf8_bytes_div4.v1`: ceil(UTF8 bytes/4)). Это оценка для offline сравнения, не гарантия влезания в модель и не реальная стоимость API. С подключением аналитика counter заменить его tokenizer и повторить сравнение. Полный projection/omission decisions сохраняются, источники/conflicts не обрезаются молча.

## 5. Метрики

- Primary **Recall@20 после fusion и token budget**: |selected∩relevant|/|relevant| в eligible test universe. Если осталось <20 событий, denominator recall прежний. До бюджета считать отдельный candidate recall, не подменять им primary.
- Macro mean по запросам с positives; micro counts дополнительно; query без positives → recall null и отдельный count, не 0/1.
- Precision@20=hits/20; precision_returned=hits/число возвращённых отдельно (null при empty). Для no-positive query precision определена, recall нет.
- nDCG@10: gain=2^label−1, discount=log2(rank+1); IDCG строится по всем judged eligible событиям запроса; IDCG0→null. Unknown внутри результата не выдаёт числовой full metric.
- Class3 recall; число новых relevant IDs относительно H3; число потерянных relevant IDs; net gain. Не объединять gains разных компаний как одну новость без company scope.
- Unique relevant coverage/strata, feature/view coverage, доля undefined labels/failed/degraded, duplicate rate, candidates до/после budget, tokens, p50/p95 latency и peak RSS. Build/model load/encoding отдельно от warm request.

Все per-query ID lists и label refs сохраняются в ignored artifacts. Результат с неполной разметкой — pooled/partial + judged coverage, не full recall. При failed channel запрещено сравнивать как нормальный вариант; report failed/degraded отдельно.

Для latency: прогреть индекс/кэш, ≥30 повторов query set на одном CPU affinity/thread config, фиксированный порядок либо одинаковый seeded shuffle; сообщить cold startup и запросы отдельно. Повторы latency не увеличивают число независимых quality samples. Для paired Δ использовать bootstrap по company_id, seed42, 2000 resamples; на малом N указать exploratory, не скрывать ширину интервала. Story-level sensitivity при расширении корпуса.

## 6. Решение о включении графа

Начальные инженерные gates, фиксируются до открытия test (не измеренные достижения):

1. Нужны human labels и полная сопоставимая оценка; temporal data допустимы для intended production use. Static diagnostic может поддержать гипотезу, но не закрывает temporal gate.
2. Macro ΔRecall@20 против H3 не менее +0.03 на общем test set, paired 95% CI нижняя граница >0. На small pilot без устойчивости сохраняем `insufficient_evidence`, а не требуем удобную положительную метку.
3. Нет новых потерь заранее зафиксированных critical class3 regression cases; Precision@20 и nDCG@10 не хуже H3 более чем на 0.02 абсолютных. Проверить отдельно good/weak/no_graph, показать все counts.
4. Warm p95 не более 2× H3 на том же hardware как начальный budget; memory укладывается в заданный deployment limit, который до deployment должен быть явно задан. Это проектный gate, не SLA сервера.
5. Provenance разрешается, unknown coverage не скрыто, graph можно отключить без смены B6/handoff, модели не смешаны.

При непрохождении — оставить base3, сохранить анализ ошибок. Не тюнить weights/thresholds на test ради прохода. R6 общая цель Recall@20>=.80 остаётся отдельной целью полезности всей системы; graph gate проверяет улучшение, а не доказывает достижение .80 автоматически.
