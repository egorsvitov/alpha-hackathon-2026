"""Cheap fallback for untagged activity matches, not semantic understanding.

Prefix normalization approximates Russian word forms. Two different specific
terms are required; explicit nonmatching scope disables this weak channel.
"""
import re

STOP_WORDS = """
деятельность прочие прочий прочими прочая организация компания компаний предприятие
синтетическая вымышленный тестовое тестовый текст сообщение новые новый новая
изменение изменения правила требований требования новости услуги услуг связанная
включая кроме между более менее также данный данная этого этом числе производство
""".split()


def stem(word):
    return word[:5] if len(word) > 5 else word


STOP = {stem(word) for word in STOP_WORDS}


def terms(text):
    return {stem(word) for word in re.findall(r"[а-яёa-z]+", (text or "").lower().replace("ё", "е"))
            if len(word) >= 5 and stem(word) not in STOP}


def activity_overlap(profile, event):
    code, industry = profile["okved"]["code"], profile["industry"]
    if event["okved_prefixes"] and not any(code and (code == p or code.startswith(p+".")) for p in event["okved_prefixes"]):
        return []
    if event["industries"] and industry not in event["industries"]:
        return []
    if event["regions"] and (not profile["region"] or profile["region"].casefold() not in {r.casefold() for r in event["regions"]}):
        return []
    overlap = terms(profile["okved"]["name"]) & terms(event["title"]+" "+event["text"])
    return sorted(overlap) if len(overlap) >= 2 else []
