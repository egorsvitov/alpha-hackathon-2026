"""CLI: python3 -m ranking demo | profile | rank."""
import argparse
import json
from pathlib import Path
import sys
from .engine import load_rules, rank_events, score_event
from .events import EventError, load_events, validate_event
from .graph import DEFAULT_PAYMENTS, attach_payment_graph
from .metrics import evaluate
from .profiles import CompanyRepository, DEFAULT_COMPANIES, ROOT

EXAMPLES = ROOT / "data/examples/ranking"
DEFAULT_OUTPUT = ROOT / "data/processed/ranking"


def save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)+"\n", encoding="utf-8")


def table_cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def demo_report(profiles, results, evaluations, as_of, graph_stats):
    lines = ["# Демонстрация персонализированного ранжирования", "",
             f"Момент анализа: {as_of}.", "",
             "Профили — настоящие обезличенные записи CSV. Все события вымышлены.",
             "Метки provisional, подготовлены тем же агентом; независимая оценка не проводилась.",
             "Баллы — эвристика, не вероятность риска и не денежный эффект.", ""]
    for profile, result, evaluation in zip(profiles, results, evaluations):
        lines += [f"## {profile['company_id']} — {profile['okved']['name']}", "",
                  f"ОКВЭД: {profile['okved']['code']}. Отрасль: {profile['industry']}. Регион: {profile['region']}.",
                  f"Выручка raw: {profile['revenue']['raw']}; единица не подтверждена.",
                  "Продукты: "+", ".join(f"{key}={value}" for key, value in profile["products"].items()), "",
                  "| Приоритет | Событие | Балл | Основание |", "|---|---|---:|---|"]
        for position, item in enumerate(result["feed"], 1):
            reasons = "; ".join(r["text"] for r in item["reasons"] if r["group"] not in {"freshness", "upcoming"})
            lines.append(f"| {position} | {table_cell(item['event']['title'])} | {item['score']:.2f} | {table_cell(reasons)} |")
        if not result["feed"]:
            lines.append("| — | Нет событий выше порога | — | Нужны дополнительные данные/правила |")
        lines += ["", f"Кандидатов: {len(result['ranked'])}; исключено сообщений: {len(result['excluded'])}.",
                  f"Граф: {profile['graph']['status']}.", "", "Черновая оценка:", "",
                  "```json", json.dumps(evaluation, ensure_ascii=False, indent=2), "```", ""]
    if graph_stats:
        lines += ["## Платежная выборка", "", "```json", json.dumps(graph_stats, ensure_ascii=False, indent=2), "```", "",
                  "Синтетические graph-probes сохранены отдельно и не включены в метрики ленты.", ""]
    lines += ["## Ограничения", "",
              "Нет LLM, эмбеддингов и raw-text extraction. Темы событий заданы вручную.",
              "Контрагенты сопоставляются только по явно переданным проверенным либо синтетическим ID.",
              "Нет периода платежей, условий договоров, операционных площадок и количественной оценки ущерба.",
              "Результаты на 48 черновых метках нельзя обобщать на реальный поток без проверки аналитиком.", ""]
    return "\n".join(lines)


def run_demo(args):
    repository = CompanyRepository(args.companies_csv)
    ids = args.company_ids or json.loads((EXAMPLES / "selection.json").read_text())["company_ids"]
    profiles = [repository.get(uid) for uid in ids]
    graph_stats = None
    if args.with_payments:
        profiles, graph_stats = attach_payment_graph(profiles, repository.company_ids, args.payments_csv)
    events = load_events(args.events)
    rules = load_rules(args.rules) if args.rules else load_rules()
    labels_doc = json.loads(Path(args.labels).read_text(encoding="utf-8"))
    results = [rank_events(profile, events, args.as_of, args.top_k, rules) for profile in profiles]
    evaluations = [evaluate(result, events, labels_doc["labels"], args.top_k) for result in results]
    out = Path(args.output)
    save(out/"profiles.json", {"profiles": profiles, "selection": "real_csv_records", "source_sha256": repository.sha256})
    save(out/"feeds.json", {"as_of": args.as_of, "events_status": "handcrafted_structured_input", "results": results})
    save(out/"evaluation.json", {"independent_benchmark": False, "evaluations": evaluations})
    if graph_stats:
        save(out/"graph_stats.json", graph_stats)
        probes = []
        for profile in profiles:
            if not profile["graph"]["neighbors"]:
                continue
            target = profile["graph"]["neighbors"][0]["company_id"]
            probe = validate_event({"event_id": "graph-probe-"+profile["company_id"],
                                   "title": "Синтетический тест платежной связи", "text": "Тест, не реальная новость.",
                                   "event_type": "counterparty", "published_at": args.as_of,
                                   "source_id": "synthetic://graph-probe", "synthetic": True,
                                   "entity_ids": [target], "entity_resolution": "synthetic"})
            probes.append({"company_id": profile["company_id"], "result": score_event(profile, probe, args.as_of, rules)})
        save(out/"graph_probes.json", {"status": "synthetic_mapping_only_not_benchmark", "probes": probes})
    report = demo_report(profiles, results, evaluations, args.as_of, graph_stats)
    (out/"demo.md").write_text(report, encoding="utf-8")
    for profile, result in zip(profiles, results):
        print(f"\n{profile['company_id']} | {profile['industry']} | {profile['okved']['name']}")
        for item in result["feed"]:
            print(f"  {item['score']:6.2f}  {item['event_id']}: {item['event']['title']}")
    print(f"\nОтчет и JSON: {out}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Профили и объяснимое ранжирование. Без LLM-вызовов.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ["demo", "profile", "rank"]:
        cmd = commands.add_parser(name)
        cmd.add_argument("--companies-csv", default=str(DEFAULT_COMPANIES))
        cmd.add_argument("--output", default=str(DEFAULT_OUTPUT) if name == "demo" else None)
        if name in {"profile", "rank"}:
            cmd.add_argument("--company-id", required=True)
        if name in {"demo", "rank"}:
            cmd.add_argument("--events", default=str(EXAMPLES/"events.json"))
            cmd.add_argument("--as-of", default="2026-10-20T18:00:00+03:00" if name == "demo" else None,
                             required=name == "rank")
            cmd.add_argument("--top-k", type=int, default=5)
            cmd.add_argument("--rules")
            cmd.add_argument("--with-payments", action="store_true")
            cmd.add_argument("--payments-csv", default=str(DEFAULT_PAYMENTS))
        if name == "demo":
            cmd.add_argument("--company-ids", nargs="+")
            cmd.add_argument("--labels", default=str(EXAMPLES/"labels.json"))
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            if args.top_k < 1:
                raise ValueError("top-k должен быть положительным")
            run_demo(args)
            return 0
        repository = CompanyRepository(args.companies_csv)
        profile = repository.get(args.company_id)
        if args.command == "profile":
            result = profile
        else:
            if args.with_payments:
                profiles, _ = attach_payment_graph([profile], repository.company_ids, args.payments_csv)
                profile = profiles[0]
            result = rank_events(profile, load_events(args.events), args.as_of, args.top_k,
                                 load_rules(args.rules) if args.rules else None)
        if args.output:
            save(args.output, result)
            print(f"Сохранено: {args.output}")
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
