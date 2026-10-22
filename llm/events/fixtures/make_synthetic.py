"""Rebuild the 20 hand-authored synthetic regression cases; no LLM is used."""

import json
from pathlib import Path

from llm.events.contracts.annotations import AnnotationBuilder, measurement as m, period as p


TEXTS = [
    "20 октября 2026 года Учебный регулятор сохранил ключевую ставку на уровне 14%.",
    "Учебный регулятор на заседании 20 октября 2026 года оставил ключевую ставку без изменения: 14%.",
    "20 октября 2026 года Учебный регулятор сохранил ключевую ставку на уровне 15%.",
    "Учебный аналитик А ожидает, что 23 октября 2026 года Учебный регулятор сохранит ключевую ставку 14%.",
    "23 октября 2026 года Учебный регулятор сохранил ключевую ставку 14%.",
    "На заседании 22 октября 2026 года Учебный регулятор не повысил ключевую ставку.",
    "1 октября 2026 года Учебное министерство предложило повысить пошлину на сталь до 20%.",
    "10 октября 2026 года Учебное министерство утвердило пошлину на сталь 20%; она начнет действовать с 1 ноября 2026 года.",
    "1 ноября 2026 года вступило в силу решение Учебного министерства о пошлине на сталь 20%.",
    "Демо-завод пересмотрел инвестиционную программу на 2026 год с 1,1 трлн до 1,096 трлн рублей.",
    "Демо-банк прогнозирует доход за 2026 год в диапазоне 185–190 млрд рублей.",
    "Демо-банк сообщил, что за январь—сентябрь 2026 года доля просрочки снизилась на 0,5 процентного пункта и составила 1,7%.",
    "В сентябре 2026 года поставки нефти из России в Китай составили 2,42 млн баррелей в сутки: на 20% больше, чем в сентябре 2025 года, и на 8,5% меньше, чем в августе 2026 года.",
    "Учебный аналитик А в базовом сценарии ожидает ставку 13,75% на конец 2026 года, а в альтернативном сценарии — 14% на конец 2026 года.",
    "За 2026 год Демо-завод увеличил выручку на 12% и сократил персонал на 8% по сравнению с 2025 годом.",
    "Учебный аналитик Б ожидает, что 23 октября 2026 года Учебный регулятор сохранит ключевую ставку 14%.",
    "27 октября 2026 года Учебный регулятор сохранил ключевую ставку 14%.",
    "Учебное министерство планирует изменить пошлину на сталь; срок и величина не названы.",
    "Заседание Учебного регулятора состоялось 19 октября 2026 года. На нем ключевую ставку сохранили на уровне 14%.",
    "📰 Исправление: 18 октября 2026 года Учебный регулятор сохранил ставку 14%. Ранее в этой публикации ошибочно указывалось 15%.",
]


def build():
    articles = [{"article_id": f"s{i:02}", "source": "Учебное издание", "url": None,
        "title": f"Синтетический случай {i:02}", "published_at": "2026-11-02T10:00:00+03:00",
        "language": "ru", "text": text, "content_kind": "synthetic",
        "is_full_original_text": False, "is_synthetic": True} for i, text in enumerate(TEXTS, 1)]
    b = AnnotationBuilder(articles, "synthetic_regression")
    regulator = {"entity_id": "demo:regulator", "name": "Учебный регулятор", "role": "decision_maker"}
    ministry = {"entity_id": "demo:ministry", "name": "Учебное министерство", "role": "authority"}

    def add(i, event, *, cid="c1", typ="policy_rate", **kw):
        aid = f"s{i:02}"
        return b.claim(aid, cid, "synthetic:" + event, typ, kw.pop("text", TEXTS[i-1]),
            kw.pop("quotes", [TEXTS[i-1]]), **kw)

    ids = {}
    for i, value in [(1, "14"), (2, "14"), (3, "15")]:
        ids[i] = add(i, "meeting20", actor=regulator, action="hold", object="policy_rate",
            occurred=p("20 октября 2026 года", "2026-10-20"), stage="approved",
            measurements=[m("policy_rate", value, "percent")], topics=["rates"], factors=["interest_rates"])
    b.bundles["synthetic:meeting20"]["relations"].append({"type":"duplicate_claim","from_id":ids[2],"to_id":ids[1],"target_kind":"claim"})
    b.bundles["synthetic:meeting20"]["conflicts"].append({"conflict_id":"synthetic:rate-conflict",
        "description":"Разные значения ставки на одном заседании.",
        "variants":[{"claim_id":ids[i],"field_path":"/measurements/0/value"} for i in [1,3]]})
    b.link(ids[1],ids[2],"duplicate_claim","Перефразирование того же решения и значения.")
    b.link(ids[1],ids[3],"conflicts_with","Установлено то же заседание, но числа несовместимы.")
    for i, author in [(4,"А"),(16,"Б")]:
        ids[i] = add(i, f"forecast-{author}", actor=regulator, attribution={"entity_id":f"demo:analyst-{author}","name":f"Учебный аналитик {author}"},
            action="hold", object="policy_rate", modality="forecast", horizon=p("23 октября 2026 года","2026-10-23"),
            measurements=[m("policy_rate","14","percent")], topics=["rates"])
    ids[5] = add(5,"meeting23",actor=regulator,action="hold",object="policy_rate",stage="approved",
        occurred=p("23 октября 2026 года","2026-10-23"),measurements=[m("policy_rate","14","percent")])
    b.link(ids[4],ids[5],"different_event","Прогноз и состоявшееся решение.")
    b.link(ids[4],ids[16],"different_event","Прогнозы разных авторов.")
    add(6,"meeting22",actor=regulator,action="raise",object="policy_rate",polarity="negated",occurred=p("22 октября 2026 года","2026-10-22"))
    for i, day, stage, modality, action in [(7,"01","proposed","proposal","raise_tariff"),(8,"10","approved","asserted","approve_tariff"),(9,None,"effective","asserted","tariff_effective")]:
        add(i,f"tariff-{stage}",typ="sanction_or_trade_restriction",actor=ministry,stage=stage,modality=modality,action=action,object="steel_tariff",
            occurred=p(f"{int(day)} октября 2026 года",f"2026-10-{day}") if day else None,
            effective=p("1 ноября 2026 года","2026-11-01") if i>=8 else None,
            measurements=[m("tariff","20","percent")],topics=["trade"],industries=["steel"],factors=["tariffs"])
    for later, earlier in [("approved","proposed"),("effective","approved")]:
        b.bundles[f"synthetic:tariff-{later}"]["relations"].append({"type":"related_stage","from_id":f"synthetic:tariff-{later}","to_id":f"synthetic:tariff-{earlier}","target_kind":"event"})
    add(10,"investment",typ="investment",action="revise",object="investment_program",observation=p("2026 год","2026-01-01","2026-12-31"),
        measurements=[m("investment_program","1100000000000","RUB",role="before"),m("investment_program","1096000000000","RUB",role="after")])
    add(11,"income-forecast",typ="company_results",modality="forecast",object="operating_income",horizon=p("2026 год","2026-01-01","2026-12-31"),
        measurements=[m("operating_income",{"min":"185000000000","max":"190000000000"},"RUB")])
    add(12,"arrears",typ="company_results",object="arrears_ratio",observation=p("январь—сентябрь 2026 года","2026-01-01","2026-09-30"),
        measurements=[m("arrears_ratio","-0.5","percentage_point",kind="absolute_change"),m("arrears_ratio","1.7","percent")])
    add(13,"oil",typ="market_indicator",object="oil_import_ru_cn",observation=p("сентябре 2026 года","2026-09-01","2026-09-30"),
        measurements=[m("oil_import_ru_cn","2420000","barrel_per_day"),
          m("oil_import_ru_cn","20","percent",kind="relative_change",comparison=p("сентябре 2025 года","2025-09-01","2025-09-30")),
          m("oil_import_ru_cn","-8.5","percent",kind="relative_change",comparison=p("августе 2026 года","2026-08-01","2026-08-31"))])
    for cid, kind, expression, value in [("c1","baseline","базовом сценарии","13.75"),("c2","alternative","альтернативном сценарии","14")]:
        add(14,"scenarios",cid=cid,modality="forecast",object="policy_rate",attribution={"entity_id":"demo:analyst-А","name":"Учебный аналитик А"},
            horizon=p("конец 2026 года","2026-12-31"),scenario={"kind":kind,"text":expression},measurements=[m("policy_rate",value,"percent")])
    b.link("s14:c1","s14:c2","same_event","Разные сценарии одного прогноза, не конфликт.")
    for cid, metric, value in [("c1","revenue","12"),("c2","headcount","-8")]:
        add(15,metric,cid=cid,typ="company_results",object=metric,observation=p("2026 год","2026-01-01","2026-12-31"),
            measurements=[m(metric,value,"percent",kind="relative_change",comparison=p("2025 годом","2025-01-01","2025-12-31"))])
    b.link("s15:c1","s15:c2","different_event","Самостоятельные показатели.")
    ids[17]=add(17,"meeting27",actor=regulator,action="hold",object="policy_rate",occurred=p("27 октября 2026 года","2026-10-27"),measurements=[m("policy_rate","14","percent")])
    b.link(ids[1],ids[17],"different_event","Даты заседаний различаются.")
    add(18,"unknown-plan",typ="sanction_or_trade_restriction",actor=ministry,modality="plan",action="change",object="steel_tariff",uncertain=True,
        note="Даты и параметры нет; соответствие другим стадиям не установлено.")
    b.bundles["synthetic:unknown-plan"]["event"]["quality"]="partial"
    b.bundles["synthetic:unknown-plan"]["limitations"].append("Неизвестны дата, направление и величина изменения.")
    b.link("s18:c1","s07:c1","uncertain","Одна тема не устанавливает идентичность.")
    quotes=["Заседание Учебного регулятора состоялось 19 октября 2026 года.","На нем ключевую ставку сохранили на уровне 14%."]
    add(19,"cross-sentence",actor=regulator,action="hold",object="policy_rate",occurred=p("19 октября 2026 года","2026-10-19"),quotes=quotes,measurements=[m("policy_rate","14","percent")])
    b.records["s20"]["article_version"]=2
    b.records["s20"]["source_id"]="s20:v2"
    b.annotations["s20"]["article_version"]=2
    add(20,"corrected",actor=regulator,action="hold",object="policy_rate",occurred=p("18 октября 2026 года","2026-10-18"),
        quotes=[TEXTS[19].split(" Ранее")[0]],measurements=[m("policy_rate","14","percent")])
    b.bundles["synthetic:corrected"]["relations"].append({"type":"updates","from_id":"s20:c1","to_id":"archive:s20:v1:c1","target_kind":"claim"})
    b.exclude("s20","Ранее в этой публикации ошибочно указывалось 15%.","Архивное ошибочное значение не является текущим свидетельством.")
    for bundle in b.bundles.values():
        bundle["event"]["updated_at"] = "2026-11-02T12:00:00+03:00"
    return b.result()


if __name__ == "__main__":
    target = Path(__file__).with_name("synthetic.json")
    target.write_text(json.dumps(build(),ensure_ascii=False,indent=2)+"\n")
    print(target)
