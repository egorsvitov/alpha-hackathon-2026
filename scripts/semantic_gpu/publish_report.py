"""Export a source-free Git edition from an existing private HTML report.

Construct the public payload from allowlists. Never copy document text, titles,
URLs, prompts, final responses, or the private Markdown protocol into Git.
"""

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path

ASSETS = Path(__file__).with_name("report")


def load_payload(path):
    html = path.read_text()
    marker = '<script id="reportData" type="application/json">'
    return json.loads(html.split(marker, 1)[1].split("</script>", 1)[0])


def pick(value, keys):
    return {key: deepcopy(value[key]) for key in keys if key in value}


def safe_tuning(tuning):
    def point(value):
        result = pick(value, ("threshold", "label", "alpha", "half_life_days"))
        if "dev" in value:
            result["dev"] = {k: v for k, v in value["dev"].items() if isinstance(v, (int, float))}
        return result
    return {"chosen": point(tuning["chosen"]), "grid": [point(v) for v in tuning["grid"]]}


def public_payload(private, notes):
    public = {"schema": "semantic-interactive-report.public.v1", "public": True,
              "neighbor_encoding": "compact.v1"}
    public["units"] = [dict(pick(u, ("unit_id", "document_id", "field", "start", "end", "publication", "groups")), text="")
                       for u in private["units"]]
    public["documents"] = {did: dict(pick(d, ("source", "published_at", "language", "content_kind",
                                                               "is_full_original_text", "is_synthetic")),
                                            title="Документ #" + did[:8], text="")
                           for did, d in private["documents"].items()}
    public["groups"] = {v: [pick(g, ("group_id", "members", "minimum_pair_score", "interpretation")) for g in groups]
                        for v, groups in private["groups"].items()}
    def compact(edge):
        return [edge["i"], *[round(edge[k], 6) if edge[k] is not None else None
                              for k in ("cosine", "gap_days", "time_multiplier", "score")]]
    public["neighbors"] = {v: [[compact(edge) for edge in edges] for edges in neighbors]
                           for v, neighbors in private["neighbors"].items()}
    for key in ("coordinates", "projection", "source_counts", "pair_changes", "provenance"):
        public[key] = deepcopy(private[key])
    public["issues"] = deepcopy(notes["issues"])
    public["reviews"] = {"status": "single_annotator_draft", "annotator": "Codex; no independent human adjudication",
                         "cases": []}
    for case in private["reviews"]["cases"]:
        public["reviews"]["cases"].append(dict(pick(case, ("case_id", "group_id", "variant", "category", "label",
            "unit_ids", "document_ids", "reviewed_unit_ids", "full_selected_fragment_review")),
            reason=notes["review_reasons"][case["case_id"]]))
    public["summaries"] = {}
    for gid, result in private["summaries"].items():
        item = pick(result, ("group_id", "variant", "status", "reason", "call_id", "job_id", "run"))
        item["assessment"] = None
        if result.get("assessment"):
            reason = next(v for k, v in notes["summary_assessments"].items() if gid.startswith(k))
            item["assessment"] = {"status": result["assessment"]["status"], "reason": reason}
        if result.get("output"):
            output = result["output"]
            item["output"] = pick(output, ("format_valid", "references_exist", "semantic_support", "group_quality",
                                          "referenced_units", "unreferenced_units"))
            summary = {"theme": "Обобщение группы #" + gid[:8], "limitations": ["Текст ответа доступен в локальной версии."]}
            for key in ("items", "differences", "contradictions"):
                summary[key] = [dict(pick(claim, ("kind", "references")),
                                      text=f"Запись {j+1}: содержание сохранено только локально.")
                                for j, claim in enumerate(output["summary"][key])]
            item["output"]["summary"] = summary
        public["summaries"][gid] = item
    public["calls"] = []
    for call in private["calls"]:
        item = pick(call, ("call_id", "group_id", "created_at", "elapsed_seconds", "status_code", "finish_reason", "usage"))
        item["request"] = pick(call["request"], ("model", "max_tokens", "thinking"))
        item["content"] = None
        item["error"] = {"code": call["error"]["code"], "message": "Сбой; подробный журнал сохранён локально."} if call.get("error") else None
        public["calls"].append(item)
    a = private["aggregate"]
    public["aggregate"] = pick(a, ("cache", "api", "manual_review", "git_commit"))
    public["aggregate"]["metrics"] = {name: pick(m, ("wall_seconds", "peak_rss_bytes", "sampled_peak_rss_bytes",
        "sampled_peak_device_mib", "sample_interval_seconds")) for name, m in a["metrics"].items()}
    public["aggregate"]["comparisons"] = {name: {"configuration": deepcopy(c["configuration"]),
        "export_bytes": c["export_bytes"], "report": pick(c["report"], ("documents", "units", "usage", "variants"))}
        for name, c in a["comparisons"].items()}
    public["thresholds"] = safe_tuning(private["thresholds"])
    public["temporal"] = safe_tuning(private["temporal"])
    public["resource_series"] = {name: pick(m, ("wall_seconds", "peak_rss_bytes", "sampled_peak_device_mib", "samples"))
                                 for name, m in private["resource_series"].items()}
    public["artifacts"] = [{"name": name, "href": name} for name in ("REPORT.md", "projection.json", "manifest.json", "issues.json")]
    return public


def report_markdown(data):
    p = data["projection"]
    cache = data["aggregate"]["cache"]["full-run"]
    api = data["aggregate"]["api"]
    lines = ["# GPU-эксперимент: смысловые группы новостей", "", "23 октября 2026. Версия для Git без текстов новостей.", "",
        "[Открыть интерактивную карту](index.html). HTML открывается локально, без сервера и сети.", "",
        "## Итог", "",
        "Обработаны 1000 заранее выбранных статей, затем весь снимок: **3621 статья, 5128 фрагментов, 5035 уникальных векторов, 26 источников**.", "",
        "Начальная экспериментальная рекомендация — semantic, threshold=0.65. Временной вариант alpha=0.4 / half-life=7 выбран по dev с правилом ничьих, но на малом test потерял полезные связи. Его лучше проверять как отдельный режим хронологии. Production-качество не доказано.", "",
        "Модель BAAI/bge-m3, ревизия `5617a9f61b028005a4858fdac845db406aefb181`, 1024D dense, FP32 CUDA, L2-normalized. Порог не является вероятностью; группы предназначены для совместного чтения, не подтверждают идентичность события.", "",
        "## Измерения", "", "| Метрика | Результат |", "|---|---:|",
        f"| Скорость уникальных представлений | {cache['texts_per_second']:.2f} текста/с |",
        f"| Сумма GPU-батчей | {cache['total_batch_seconds']:.3f} с |",
        f"| Кэш векторов | {cache['bytes']/2**20:.2f} MiB |",
        "| Peak RSS embedding | около 3.44 GiB |", "| CUDA allocated / reserved, sample | 4.11 / 6.57 GiB |",
        "| Sampled device peak, sample / full | 7009 / 6943 MiB |",
        "| Full compare wall / RSS | 26.700 с / 1.23 GiB |", "| Model load из готового кэша | 3.60–3.89 с |",
        "| Холодная загрузка с сетевыми сбоями | около 21 мин 24 с |", "",
        "Скорость включает нормализацию и запись кэша, не скачивание и model load. Полный corpus-run импортировал sample-кэш и досчитал 3404 вектора. Replay обоих наборов дал 0 новых векторов. GPU ускоряет embedding; SciPy complete-link работает на CPU. Опрос device/RSS раз в секунду может пропускать короткие пики; это не CUDA allocator. OOM не было.", "",
        "## Смысл и время", "", "| Полный корпус | Групп | Синглтонов |", "|---|---:|---:|",
        "| Semantic | 1563 | 719 |", "| Semantic + time | 1865 | 881 |", "",
        "На dev 16 полезных / 4 отрицательных / 2 неопределённых пары; threshold 0.65 сохранил 7/16 полезных, 0/4 отрицательных. Пороги 0.75 и 0.85 сохранили 3/16 и 2/16. Проверены девять временных комбинаций; пять дали 7/16, выбран режим 0.4/7 правилом ничьих. Test при настройке не использован.", "",
        "На test: 10 полезных, 5 отрицательных, 1 неопределённая пара. В выборке semantic сохранил 5/10, time 2/10; на полном корпусе 4/10 и 1/10. Оба объединили 0/5 отрицательных. Такой малый набор не доказывает precision всего корпуса. Неразмеченные пары неизвестны, не отрицательные.", "",
        "Временной вариант потерял 80 849 внутригрупповых пар и добавил 1 647: изменение порядка complete-link не гарантирует вложенность. Близкий сосед может остаться в другой группе из-за худшей пары со всей группой. Publication time не подменяет event time.", "",
        "## Качество и API", "",
        "Оценены 22 dev-пары + 16 test-пар + 24 диагностических случая групп. Для 24 групп: 18 полезны для совместного чтения, 2 пропущенные связи, 4 неопределённых. Самая большая группа проверена лишь в 6 из 217 фрагментов. Все оценки — single_annotator_draft от Codex, без независимого человеческого арбитража.", "",
        f"API: {api['calls']} попыток, 67 HTTP 200, 2 транспортные/локальные ошибки, 59 формально валидных результатов, 8 finish_reason=length; 5 крупных групп не отправлены. {api['usage']['total_tokens']} токена: {api['usage']['prompt_tokens']} input + {api['usage']['completion_tokens']} output. Сумма usage.cost = {api['provider_cost_sum']}; **валюта не подтверждена, это не сумма в рублях**.", "",
        "После default-профиля проверен thinking-disabled, затем компактный формат дал 51 валидный ответ на других группах. Это непарный опыт, не доказательство причинного улучшения. Запросы не повторялись скрыто; один общий журнал с атомарным cap=200. Пять больших контекстов пропущены целиком, не усечены.", "",
        "Шесть обобщений прочитаны отдельно: одно поддержано, одно с оговорками, три частично, одно с неподтверждёнными цитатами. Существование ссылок не доказывает смысловую поддержку. Диагностический отбор не даёт популяционную оценку качества.", "",
        "## Карта и воспроизводимость", "",
        "PCA50 randomized → t-SNE, perplexity=40, max_iter=1200, init=pca, learning_rate=auto, seed=20261023; 4 CPU-потока. PCA-вид использует первые две компоненты того же fit. Координаты не зависят от группировки.", "",
        f"Trustworthiness@10 к исходному 1024D cosine: t-SNE {p['metrics']['tsne']['trustworthiness_k10']:.4f}, PCA {p['metrics']['pca']['trustworthiness_k10']:.4f}. Recall десятки соседей: {p['metrics']['tsne']['neighbor_recall_k10']*100:.2f}% и {p['metrics']['pca']['neighbor_recall_k10']*100:.2f}%. PCA2 объясняет {p['pca_variance_2d']*100:.2f}% дисперсии. Все 5128 точек оценены, self исключён; копии оставлены, равные ранги разрешаются стабильным порядком.", "",
        "[Методика t-SNE](https://scikit-learn.org/stable/modules/generated/sklearn.manifold.TSNE.html), [trustworthiness](https://scikit-learn.org/stable/modules/generated/sklearn.manifold.trustworthiness.html). Острова, плотность и 2D-расстояния не являются вероятностями и не заменяют исходные scores.", "",
        "Версия для Git сохраняет координаты, IDs, даты/источники, membership, округлённые до 6 знаков scores соседей, метрики и обезличенные диагностические заметки. Заголовки, тексты, URL новостей, prompts, финальные API-ответы, ключи и базы отсутствуют. Проверка цитат по тексту доступна только в полной локальной версии.", "",
        "Сборка: `scripts/semantic_gpu/publish_report.py`. Публичные заметки: `scripts/semantic_gpu/report/public_notes.json`. Для пересборки из приватных артефактов нужен локальный HTML, а не доступ к API. `projection.json`, `manifest.json`, `issues.json` фиксируют координаты, хэши и диагностику.", "",
        "## Ошибки, гипотезы и предложения", ""]
    for issue in data["issues"]:
        lines.extend([f"### {issue['id']} · {issue['priority']} · {issue['title']}", "", f"Статус: {issue['status']}.", "",
                      f"Наблюдение: {issue['observed']}", "", f"Гипотеза: {issue['hypothesis']}", "",
                      f"Предложение: {issue['fix']}", "", f"Как проверить: {issue['test']}", ""])
    return "\n".join(lines)


def publish(source, output):
    if not output.resolve().is_relative_to((Path.cwd() / "docs/reports").resolve()):
        raise ValueError("Git edition must be under docs/reports/")
    private = load_payload(source)
    notes = json.loads((ASSETS / "public_notes.json").read_text())
    data = public_payload(private, notes)
    data["report_text"] = report_markdown(data)
    output.mkdir(parents=True, exist_ok=True)
    (output / "REPORT.md").write_text(data["report_text"])
    (output / "issues.json").write_text(json.dumps(data["issues"], ensure_ascii=False, indent=2) + "\n")
    projection = dict(deepcopy(data["projection"]), coordinates=data["coordinates"],
                      unit_ids=[u["unit_id"] for u in data["units"]])
    (output / "projection.json").write_text(json.dumps(projection, separators=(",", ":")))
    serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    html = (ASSETS / "index.html").read_text()
    html = html.replace("/*__STYLE__*/", (ASSETS / "style.css").read_text())
    html = html.replace("/*__DATA__*/", serialized).replace("/*__APP__*/", (ASSETS / "app.js").read_text())
    html = html.replace("<main>", '<main><p class="notice">Версия для Git: исходные тексты и заголовки новостей, URL, prompts и ответы API остаются только в локальном отчёте. Здесь доступны карта, метаданные, оценки и разбор ошибок.</p>', 1)
    (output / "index.html").write_text(html)
    manifest = {"schema": data["schema"], "units": len(data["units"]), "documents": len(data["documents"]),
                "private_report_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "builder_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "files": {name: hashlib.sha256((output / name).read_bytes()).hexdigest() for name in
                          ("index.html", "REPORT.md", "projection.json", "issues.json")},
                "excluded": ["article text/title/URL", "unit text/embedding_text", "API messages and content",
                             "raw responses/headers/reasoning", "private protocol", "keys", "databases"],
                "neighbor_scores_decimal_places": 6}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"output": str(output), "html_bytes": (output / "index.html").stat().st_size,
                      "units": manifest["units"], "documents": manifest["documents"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("data/processed/event_pipeline/interactive-report-20261023/index.html"))
    parser.add_argument("--output", type=Path, default=Path("docs/reports/semantic-gpu-20261023"))
    args = parser.parse_args()
    publish(args.source, args.output)
