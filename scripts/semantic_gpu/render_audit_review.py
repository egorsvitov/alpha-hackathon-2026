"""Render a local source-reading report from audited packets and annotations."""

import argparse
from collections import Counter
import hashlib
from html import escape
import json
import os
from pathlib import Path


def read(path):
    return json.loads(path.read_text())


def render(root, atlas):
    if not root.resolve().is_relative_to((Path.cwd() / "data/processed/event_pipeline").resolve()):
        raise ValueError("Source-bearing report must remain in ignored data/")
    audit = read(root / "coverage-audit.json")
    reviews = read(root / "review/annotations.json")
    packets = {c["case_id"]: c for c in read(root / "review/packets.json")}
    replay = read(root / "metrics/all-news-cache-recheck-result.json")
    metrics = read(root / "metrics/full-recheck.json")
    manifest = read(root / "comparisons/full-recheck/manifest.json")
    labels = {"useful_together": "Полезно читать вместе", "uncertain": "Сомнительно / смешанная группа",
              "missing_link": "Пропущенная связь", "unrelated_material": "Лишнее объединение"}
    atlas_href = os.path.relpath(atlas, root)
    summary = [
        f"Весь доступный prepared-news.v1: {audit['corpus_documents']} статья, {audit['units']} фрагментов, {audit['unique_embedding_texts']} уникальных представлений, {len(audit['sources'])} источников. Нового локального корпуса не обнаружено.",
        f"Повторный embed: complete={replay['complete']}, new_vectors={replay['new_vectors']}, remaining={replay['remaining']}. Уже рассчитанные представления переиспользованы, повторного inference не было. Model load {replay['load_seconds']:.3f} с, проверка кэша {replay['inference_wall_seconds']:.3f} с; это не новая скорость inference.",
        f"Свежая группировка: wall {metrics['wall_seconds']:.3f} с, peak RSS {metrics['peak_rss_bytes']/2**30:.3f} GiB. Все семь экспортов документов, фрагментов, векторов, групп и соседей побайтно совпали с прошлым запуском. Времена и manifest записаны заново.",
        "Механическая проверка: пропусков непустого исходного текста нет; каждый unit имеет один вектор и одну группу в каждом режиме. Все внутригрупповые пары проходят порог 0.65; все 102560 соседских scores пересчитаны и совпали с точностью 1e-10. SQLite integrity_check=ok. Это корректность расчёта, а не доказательство смысла групп.",
        "Использована BAAI/bge-m3, ревизия 5617a9f61b028005a4858fdac845db406aefb181, dense 1024D, FP32 CUDA. Semantic=max(0,cosine); time-score=semantic×(0.6+0.4×2^(−gap/7)). Порог 0.65, параметры после чтения новых примеров не менялись.",
        "Новых API-обобщений не запускал: этот проход проверяет эмбеддинги, членство и исходные тексты. Сводки предыдущего опыта не использованы как эталон.",
        "Новые восемь групп не пересекаются с предыдущими 24 проверенными ни по ID групп, ни по выбранным units. Отбор диагностический после просмотра метаданных/отрывков; это не случайная репрезентативная выборка и не независимый test."
    ]
    lines = ["# Все новости: повторный полный проход и новый разбор", "", "23 октября 2026. Локальный отчёт с исходными новостями — вне Git.", ""]
    lines.extend(paragraph for text in summary for paragraph in (text, ""))
    lines += [reviews["scope"], "", "Оценки — **черновик Codex по чтению источников**, без независимого человеческого арбитража. Проверялось содержание предоставленных текстов, не истинность сообщений и не полнота оригинальных веб-страниц.", "",
              "## Структура полного корпуса", "", "| Вариант | Групп | Синглтонов | Самая большая | Межисточниковых | Проверено пар внутри групп |", "|---|---:|---:|---:|---:|---:|"]
    for v, d in audit["variants"].items():
        lines.append(f"| {v} | {d['groups']} | {d['singletons']} | {d['largest_group']} | {d['cross_source_groups']} | {d['within_group_pairs_checked']} |")
    lines += ["", "Уменьшение крупнейшей группы с 217 до 35 и рост числа групп не доказывают улучшение качества: ниже есть и полезные, и вредные временные разделения.", "", "## Восемь новых случаев", ""]
    cards = []
    for note in reviews["cases"]:
        packet = packets[note["case_id"]]
        assert set(note["reviewed_unit_ids"]) == {u["unit_id"] for u in packet["units"]}
        lines += [f"### {note['case_id']}. {note['title']}", "", f"**{note['verdict']}** · {labels[note['reading_label']]}", ""]
        fields = [("Что проверено", "finding"), ("Одно ли событие", "event_identity"), ("Что изменило время", "time_effect"),
                  ("Гипотеза причины", "hypothesis"), ("Что делать", "proposal"), ("Граница проверки", "scope_note")]
        body = []
        for label, key in fields:
            lines.extend([f"**{label}.** {note[key]}", ""])
            body.append(f"<p><strong>{escape(label)}.</strong> {escape(note[key])}</p>")
        lines += [f"Группа `{note['group_id']}`.", "", "| Источник / документ | Публикация | Unit / координаты | Карта |", "|---|---|---|---|"]
        source_html = []
        for unit in packet["units"]:
            doc = packet["documents"][unit["document_id"]]["article"]
            link = atlas_href + "#unit=" + unit["unit_id"]
            lines.append(f"| {doc['source']} / `{unit['document_id']}` | {unit['publication']['value']} | `{unit['unit_id']}` · {unit['field']}[{unit['start']}:{unit['end']}] | [Открыть]({link}) |")
            source_html.append(f'<details><summary>{escape(doc["source"])} · {escape(doc["title"])} · {unit["publication"]["value"]}</summary><p class="muted">unit {unit["unit_id"]} · {unit["field"]}[{unit["start"]}:{unit["end"]}]</p><a class="source-link" href="{escape(link,quote=True)}" target="_blank" rel="noopener">Этот фрагмент на карте ↗</a><pre class="original">{escape(unit["text"])}</pre></details>')
        lines += ["", "| Пара units | Cosine | Gap, дней | Time-score | Вместе S / T |", "|---|---:|---:|---:|---|"]
        pair_html = []
        for pair in packet["pair_scores"]:
            short_pair = pair["left"][:8] + " / " + pair["right"][:8]
            gap = f"{pair['gap_days']:.4f}" if pair["gap_days"] is not None else "unknown"
            together = f"{pair['same_semantic_group']} / {pair['same_time_group']}"
            lines.append(f"| {short_pair} | {pair['cosine']:.4f} | {gap} | {pair['score']:.4f} | {together} |")
            pair_html.append(f"<tr><td>{short_pair}</td><td>{pair['cosine']:.4f}</td><td>{gap}</td><td>{pair['score']:.4f}</td><td>{together}</td></tr>")
        lines.append("")
        for unit in packet["units"]:
            lines += [f"<details><summary>Исходный фрагмент {unit['unit_id'][:12]}</summary>", "", "```text", unit["text"], "```", "", "</details>", ""]
        cards.append(f'<article class="card manual-case" id="{note["case_id"]}" data-label="{note["reading_label"]}"><span class="eyebrow">{note["case_id"]} · #{note["group_id"][:12]}</span><h2>{escape(note["title"])}</h2><p class="notice"><strong>{escape(note["verdict"])}</strong> · {labels[note["reading_label"]]}</p>{"".join(body)}<h3>Прочитанные фрагменты</h3>{"".join(source_html)}<details><summary>Точные scores и членство всех выбранных пар</summary><div class="table-wrap"><table><thead><tr><th>Пара</th><th>Cosine</th><th>Gap, дней</th><th>Time-score</th><th>Вместе S / T</th></tr></thead><tbody>{"".join(pair_html)}</tbody></table></div></details></article>')
    conclusions = [
        "Эмбеддинги и complete-link вычислены без найденных технических потерь, но готовое разбиение нельзя считать каталогом одного события: тематическая близость смешивает стадии, юрисдикции и метрики.",
        "Сначала распознавать дайджесты/справочные страницы, проверять даты и качество HTML-extraction; сохранять исходник и offsets. Для очищенной версии нужен новый plan и отдельный кэш-профиль.",
        "Добавить признаки объекта, юрисдикции, типа показателя и модальности. Не считать отличающиеся числа противоречием без совпадающих условий измерения.",
        "Хранить related-граф наряду с membership: банковская новость уже находит правильный сосед, но не помещается в большую группу целиком. Проверить иерархию по разделам длинных документов.",
        "Время помогло разделить два сомнительных объединения, но разорвало полезную погодную связь. Сначала проверять происхождение дат; временной режим не объявлять универсальным улучшением.",
        "Не перенастраивать порог по восьми удобным случаям. Следующий сравнительный эксперимент проводить на заранее замороженной сюжетно независимой разметке, включая трудные отрицательные пары и проверку цитат."
    ]
    lines += ["## Выводы и приоритеты", ""]
    lines += [f"{i}. {text}" for i, text in enumerate(conclusions, 1)]
    lines += ["", "## Артефакты и воспроизведение", "", "`coverage-audit.json`: проверка всех документов/units/пар/соседей; `review/selection.json`: зафиксированный отбор; `review/packets.json`: источники и scores; `review/annotations.json`: оценки и границы чтения; `review/date-counterfactual.json`: гипотетическая проверка даты без изменения данных.", "",
              "`scripts/semantic_gpu/audit_corpus.py` и `render_audit_review.py` воспроизводят аудит и этот отчёт. Новые замеры — в `metrics/`, свежий неизменяемый экспорт — в `comparisons/full-recheck/`. Параметры берутся из его manifest. Все исходные новости, рабочие базы и API-ключи остаются вне Git.", ""]
    (root / "MANUAL_REVIEW.md").write_text("\n".join(lines))
    style = Path(__file__).with_name("report").joinpath("style.css").read_text()
    paragraphs = "".join(f"<p>{escape(text)}</p>" for text in summary)
    options = '<option value="">Все восемь случаев</option>' + "".join(f'<option value="{k}">{v}</option>' for k,v in labels.items())
    counts = Counter(n["reading_label"] for n in reviews["cases"])
    html = f'''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'none'; base-uri 'none'"><title>Все новости · восемь новых разборов</title><style>{style}\n.manual-case{{margin:22px 0}}.manual-case h2{{margin:10px 0 18px}}.manual-case p{{color:#415844;line-height:1.8}}.manual-case details{{border-top:1px solid #d8e2cf;padding:12px 0}}.manual-case summary{{cursor:pointer;font-size:12px;color:#57784f}}.manual-case .original{{max-height:420px;font-size:13px}}.source-link{{display:inline-block;margin:8px 0}}.summary-block{{font-size:13px;line-height:1.8}}.kpis{{margin-bottom:25px}}[hidden]{{display:none!important}}</style><header class="masthead"><span class="brand">СМЫСЛОВОЙ АТЛАС / ПОЛНЫЙ ПОВТОРНЫЙ АУДИТ</span><span class="stamp">23.10.2026 · локальные источники</span></header><main><div class="intro"><div><div class="eyebrow">3621 СТАТЬЯ / 8 НОВЫХ СЛУЧАЕВ</div><h1>Расчёт корректен. А смысл?</h1><p>Повторный полный проход и чтение выбранных исходных фрагментов.</p></div></div><div class="kpis"><div class="kpi"><div class="value">5128</div><div class="label">проверенных векторов</div></div><div class="kpi"><div class="value">0</div><div class="label">пропущенных фрагментов</div></div><div class="kpi"><div class="value">102560</div><div class="label">проверенных соседских scores</div></div><div class="kpi"><div class="value">24 / 23</div><div class="label">прочитанных фрагмента / документа</div></div><div class="kpi"><div class="value">8</div><div class="label">новых разборов групп</div></div></div><div class="notice">Черновик Codex по чтению источников, без независимого человеческого арбитража. Проверены выбранные тексты, а не истинность новостей. Диагностический отбор не даёт оценку качества всего корпуса. Исходные тексты остаются вне Git.</div><section class="card summary-block"><h2>Что выполнено</h2>{paragraphs}<p><strong>Граница чтения:</strong> 24 фрагмента из 23 документов: 21 предоставленный текст прочитан полностью; длинная статья и коммюнике — только в выбранных частях.</p><p>Semantic: 1563 группы / 719 одиночек, максимум 217. Time: 1865 / 881, максимум 35. Найденных нарушений заданных математических правил — ноль.</p><p>Состав диагностической выборки: {escape(str(dict(counts)))}. Это описательные количества, не population precision.</p><a class="source-link" href="{escape(atlas_href,quote=True)}">Открыть общую карту эмбеддингов ↗</a> · <a class="source-link" href="MANUAL_REVIEW.md">Markdown-отчёт ↗</a></section><div class="section-heading audit-heading"><h2>Разбор по исходникам</h2><label>Оценка<select id="filter">{options}</select></label></div>{"".join(cards)}<section class="card"><h2>Что исправлять в первую очередь</h2><ol class="method-list">{"".join(f"<li>{escape(t)}</li>"for t in conclusions)}</ol></section><footer><span>Полный локальный аудит · исходные данные не публикуются</span><span>BAAI/bge-m3 · threshold 0.65 · α 0.4 / 7 дней</span></footer></main><script>document.getElementById('filter').onchange=function(){{for(const card of document.querySelectorAll('.manual-case'))card.hidden=this.value!==''&&card.dataset.label!==this.value;}};</script></html>'''
    (root / "index.html").write_text(html)
    files = ["coverage-audit.json", "review/selection.json", "review/packets.json", "review/annotations.json",
             "review/date-counterfactual.json", "MANUAL_REVIEW.md", "index.html"]
    hashes = {name: hashlib.sha256((root/name).read_bytes()).hexdigest() for name in files}
    (root / "review-manifest.json").write_text(json.dumps({"files": hashes, "comparison_manifest": manifest,
        "renderer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "scope": reviews["scope"]}, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"report": str(root/"index.html"), "cases": len(cards), "labels": counts}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("data/processed/event_pipeline/full-corpus-audit-20261023"))
    parser.add_argument("--atlas", type=Path, default=Path("data/processed/event_pipeline/interactive-report-20261023/index.html"))
    args = parser.parse_args()
    render(args.root, args.atlas)
