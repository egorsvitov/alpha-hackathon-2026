"""Анализ целевой аудитории из CSV-файлов для формирования тематики батчей.

Задача: на основе предоставленных обезличенных данных о клиентах (юрлица,
связи с физлицами, платежи) определить, какие отрасли, регионы, страны ВЭД и
типы клиентов преобладают у целевой аудитории. Это позволяет формировать
регулярные батчи новостей, релевантные реальным клиентам, а не собирать всё
подряд.

Данные НЕ содержат реальных идентификаторов (обезличены) и НЕ пушатся в git
(лежат в news/*.csv, игнорируются). Этот модуль читает их локально.

CSV разделены точкой с запятой (;), не запятой.
"""
from __future__ import annotations

import collections
import csv
from dataclasses import dataclass, field
from pathlib import Path

# Колонки df_Uid_data_hack.csv (юрлица).
UID_HEADER = [
    "SYNTHETIC_U", "ORG_TYPE", "OKFS_NAME", "COMPANY_AGE_YEARS_GRADATION",
    "ECO_MARK_LABEL", "MAINOKVED_CODE", "MAINOKVED_NAME", "NEGATIVEMARK",
    "AMOUNT_WORKERS_FNS_GRADATION", "AMOUNT_WORKERS_SPARK_GRADATION", "CL_TYPE",
    "CL_PRODUCT_RKO_MARK", "CL_PRODUCT_GAR_MARK", "CL_PRODUCT_LOA_MARK",
    "CL_PRODUCT_LEA_MARK", "CL_PRODUCT_DEP_MARK", "CL_PRODUCT_ZPP_MARK",
    "FTS_FLAG", "FTS_COUNTRY_NAME", "FACTORING_MARK", "REVENUE_COMPANY",
    "GROUP_ID", "CITY", "REGION",
]
FID_HEADER = ["SYNTHETIC_F", "CITY_NAME", "RC_NAME", "SYNTHETIC_U"]
PAYMENT_HEADER = ["BENEFICIARY", "PAYER", "PAY_AMT", "PAY_CNT"]


@dataclass
class AudienceProfile:
    """Тематический профиль целевой аудитории, вычисленный из CSV."""

    total_companies: int = 0
    active_clients: int = 0
    prospects: int = 0
    close_clients: int = 0
    top_industries: list[tuple[str, int]] = field(default_factory=list)   # MAINOKVED_NAME
    top_industry_codes: list[tuple[str, int]] = field(default_factory=list)  # MAINOKVED_CODE
    top_regions: list[tuple[str, int]] = field(default_factory=list)
    top_cities: list[tuple[str, int]] = field(default_factory=list)
    top_trade_countries: list[tuple[str, int]] = field(default_factory=list)  # FTS_COUNTRY_NAME
    fts_share: float = 0.0            # доля компаний с внешнеторговой активностью
    payment_volume: float = 0.0       # суммарный объём платежей (млрд ₽)
    payment_count: int = 0
    group_count: int = 0

    def summary(self) -> str:
        lines = [
            f"Компаний всего: {self.total_companies}",
            f"  действующих клиентов: {self.active_clients}",
            f"  перспективных: {self.prospects}",
            f"  закрытых: {self.close_clients}",
            f"Групп компаний: {self.group_count}",
            f"Доля с ВЭД (ФТС): {self.fts_share:.1%}",
            f"Объём платежей: {self.payment_volume:.1f} млрд ₽ ({self.payment_count} операций)",
        ]
        lines.append("Топ отраслей:")
        lines += [f"  {c:6d}  {n}" for n, c in self.top_industries[:10]]
        lines.append("Топ регионов:")
        lines += [f"  {c:6d}  {n}" for n, c in self.top_regions[:8]]
        lines.append("Топ стран ВЭД (ФТС):")
        lines += [f"  {c:6d}  {n}" for n, c in self.top_trade_countries[:8]]
        return "\n".join(lines)


def _read_csv(path: str | Path, delimiter: str = ";") -> list[list[str]]:
    p = Path(path)
    if not p.exists():
        return []
    with open(p, encoding="utf-8-sig") as fh:
        return [row for row in csv.reader(fh, delimiter=delimiter) if row]


def analyze_audience(uid_csv: str | Path,
                     fid_csv: str | Path | None = None,
                     payment_csv: str | Path | None = None) -> AudienceProfile:
    """Вычисляет профиль аудитории из CSV. Отсутствующие файлы пропускаются."""
    prof = AudienceProfile()

    rows = _read_csv(uid_csv)
    idx = {h: i for i, h in enumerate(UID_HEADER)}
    okved_name = collections.Counter()
    okved_code = collections.Counter()
    region = collections.Counter()
    city = collections.Counter()
    fts_country = collections.Counter()
    fts_companies = 0
    groups = set()

    for r in rows:
        if not r or r[0].startswith("SYNTHETIC_U"):
            continue
        prof.total_companies += 1
        cl = r[idx["CL_TYPE"]] if len(r) > idx["CL_TYPE"] else ""
        if cl == "CLIENT":
            prof.active_clients += 1
        elif cl == "PROSPECT":
            prof.prospects += 1
        elif cl == "CLOSE_CLIENT":
            prof.close_clients += 1
        name = r[idx["MAINOKVED_NAME"]] if len(r) > idx["MAINOKVED_NAME"] else ""
        code = r[idx["MAINOKVED_CODE"]] if len(r) > idx["MAINOKVED_CODE"] else ""
        if name:
            okved_name[name] += 1
        if code:
            okved_code[code] += 1
        reg = r[idx["REGION"]] if len(r) > idx["REGION"] else ""
        if reg:
            region[reg] += 1
        cit = r[idx["CITY"]] if len(r) > idx["CITY"] else ""
        if cit:
            city[cit] += 1
        fts = r[idx["FTS_FLAG"]] if len(r) > idx["FTS_FLAG"] else ""
        if fts in ("1.0", "1", 1):
            fts_companies += 1
        fc = r[idx["FTS_COUNTRY_NAME"]] if len(r) > idx["FTS_COUNTRY_NAME"] else ""
        if fc:
            # страны через запятую; считаем каждую.
            for c in [x.strip() for x in fc.split(",") if x.strip()]:
                fts_country[c] += 1
        g = r[idx["GROUP_ID"]] if len(r) > idx["GROUP_ID"] else ""
        if g:
            groups.add(g)

    prof.top_industries = okved_name.most_common()
    prof.top_industry_codes = okved_code.most_common()
    prof.top_regions = region.most_common()
    prof.top_cities = city.most_common()
    prof.top_trade_countries = fts_country.most_common()
    prof.fts_share = (fts_companies / prof.total_companies) if prof.total_companies else 0.0
    prof.group_count = len(groups)

    if payment_csv:
        pay_rows = _read_csv(payment_csv)
        pidx = {h: i for i, h in enumerate(PAYMENT_HEADER)}
        amt = 0.0
        cnt = 0
        for r in pay_rows:
            if not r or r[0].startswith("BENEFICIARY"):
                continue
            try:
                amt += float(r[pidx["PAY_AMT"]])
            except (ValueError, IndexError):
                pass
            try:
                cnt += int(float(r[pidx["PAY_CNT"]]))
            except (ValueError, IndexError):
                pass
        prof.payment_volume = amt / 1e9  # млрд ₽
        prof.payment_count = cnt

    return prof