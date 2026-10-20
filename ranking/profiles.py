"""Normalize supplied CSV records without inventing financial attributes."""
import csv
import hashlib
import math
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COMPANIES = ROOT / "data/companies/df_Uid_data_hack.csv"
PRODUCT_FIELDS = {
    "rko": "CL_PRODUCT_RKO_MARK", "guarantee": "CL_PRODUCT_GAR_MARK",
    "loan": "CL_PRODUCT_LOA_MARK", "leasing": "CL_PRODUCT_LEA_MARK",
    "deposit": "CL_PRODUCT_DEP_MARK", "payroll": "CL_PRODUCT_ZPP_MARK",
    "factoring": "FACTORING_MARK",
}


class ProfileError(ValueError):
    pass


def industry_for(code):
    """Authored broad grouping of OKVED divisions; not a full official classifier."""
    if not re.fullmatch(r"\d{2}(?:\.\d{1,2}){0,2}", code or ""):
        return None
    division = int(code[:2])
    for low, high, name in [(1, 3, "agriculture"), (5, 9, "mining"),
                            (10, 33, "manufacturing"), (35, 39, "utilities"),
                            (41, 43, "construction"), (45, 47, "trade"),
                            (49, 53, "transport"), (55, 56, "hospitality"),
                            (58, 63, "information"), (64, 66, "finance"),
                            (68, 68, "real_estate")]:
        if low <= division <= high:
            return name
    return None


def parse_flag(value):
    if type(value) is bool:
        return value
    value = str(value if value is not None else "").strip()
    if value in {"1", "1.0"}:
        return True
    if value in {"0", "0.0"}:
        return False
    return None


class CompanyRepository:
    def __init__(self, path=DEFAULT_COMPANIES):
        self.path = Path(path)
        if not self.path.exists():
            raise ProfileError(f"Нет CSV компаний: {self.path}. Получите исходные данные хакатона.")
        self.sha256 = hashlib.sha256(self.path.read_bytes()).hexdigest()
        try:
            self.source_path = str(self.path.resolve().relative_to(ROOT))
        except ValueError:
            self.source_path = str(self.path.resolve())
        self.rows, self.conflicts, self.duplicates = {}, set(), set()
        with self.path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream, delimiter=";")
            if len(reader.fieldnames or []) != len(set(reader.fieldnames or [])):
                raise ProfileError("Повторяющиеся заголовки CSV")
            required = {"SYNTHETIC_U", "MAINOKVED_CODE", "MAINOKVED_NAME", *PRODUCT_FIELDS.values()}
            absent = required - set(reader.fieldnames or [])
            if absent:
                raise ProfileError(f"В CSV отсутствуют поля: {sorted(absent)}")
            for index, raw in enumerate(reader, 1):
                if None in raw:
                    raise ProfileError(f"Лишние поля CSV, запись {index}")
                uid = str(raw.get("SYNTHETIC_U") or "").strip()
                if not uid:
                    raise ProfileError(f"Пустой SYNTHETIC_U, запись {index}")
                if uid in self.rows:
                    self.duplicates.add(uid)
                    if self.rows[uid][0] != raw:
                        self.conflicts.add(uid)
                else:
                    self.rows[uid] = (raw, index)

    @property
    def company_ids(self):
        return set(self.rows)

    def get(self, company_id):
        if company_id in self.conflicts:
            raise ProfileError(f"Конфликтующие записи компании {company_id}; произвольный выбор запрещен.")
        if company_id not in self.rows:
            raise ProfileError(f"Компания {company_id} не найдена.")
        raw, record = self.rows[company_id]
        missing, warnings, features = [], [], {}

        def feature(path, value, column, quality="observed"):
            fid = f"{company_id}:{path}"
            if value is None:
                missing.append(path)
                quality = "unknown"
            features[fid] = {"feature_id": fid, "path": path, "value": value,
                             "quality": quality, "source": {"file": self.source_path,
                             "file_sha256": self.sha256, "data_record_number": record,
                             "column": column, "raw_value": raw.get(column)}}
            return value

        feature("company_id", company_id, "SYNTHETIC_U")
        code = feature("okved.code", str(raw.get("MAINOKVED_CODE") or "").strip() or None, "MAINOKVED_CODE")
        activity_name = feature("okved.name", str(raw.get("MAINOKVED_NAME") or "").strip() or None, "MAINOKVED_NAME")
        industry = feature("industry", industry_for(code), "MAINOKVED_CODE", "derived")
        if industry is None:
            warnings.append("industry_unmapped: укрупненная отрасль не определена")
        products = {name: feature(f"products.{name}", parse_flag(raw.get(field)), field)
                    for name, field in PRODUCT_FIELDS.items()}
        revenue = None
        try:
            parsed = float(raw.get("REVENUE_COMPANY", ""))
            if math.isfinite(parsed) and parsed >= 0:
                revenue = parsed
        except (ValueError, TypeError):
            pass
        countries = sorted({s.strip().upper() for s in str(raw.get("FTS_COUNTRY_NAME") or "").split(",") if s.strip()})
        fts = feature("fts_present", parse_flag(raw.get("FTS_FLAG")), "FTS_FLAG")
        if fts is True and not countries:
            missing.append("countries")
        if fts is False and countries:
            warnings.append("fts_country_flag_conflict")
        region = feature("region", str(raw.get("REGION") or "").strip() or None, "REGION")
        workers = feature("employees_band", str(raw.get("AMOUNT_WORKERS_FNS_GRADATION") or "").strip() or None,
                          "AMOUNT_WORKERS_FNS_GRADATION")
        feature("countries", countries if countries else None, "FTS_COUNTRY_NAME",
                "conflicting" if fts is False and countries else "observed")
        feature("revenue.raw", revenue, "REVENUE_COMPANY")
        if company_id in self.duplicates:
            warnings.append("identical_duplicate_records_collapsed")
        return {
            "company_id": company_id, "profile_version": f"csv-v1:{self.sha256[:12]}",
            "synthetic": False, "okved": {"code": code, "name": activity_name},
            "industry": industry, "products": products, "region": region,
            "city": raw.get("CITY") or None, "countries": countries, "fts_present": fts,
            "revenue": {"raw": revenue, "unit": None, "unit_status": "unconfirmed"},
            "employees_band": workers, "age_band": raw.get("COMPANY_AGE_YEARS_GRADATION") or None,
            "development_stage": raw.get("ECO_MARK_LABEL") or None,
            "group_id": raw.get("GROUP_ID") or None, "client_type": raw.get("CL_TYPE") or None,
            "graph": {"status": "not_loaded"}, "features": features,
            "missing_fields": sorted(set(missing)),
            "warnings": warnings + ["revenue_unit_unconfirmed", "products_describe_supplied_bank_record_only"],
        }
