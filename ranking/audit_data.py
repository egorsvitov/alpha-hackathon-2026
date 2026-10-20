"""Read-only CSV audit. Python standard library; run from the repository root."""
import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path


def read(name):
    with (Path("data/companies") / name).open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


def summary(rows):
    return {
        "rows": len(rows),
        "columns": list(rows[0]),
        "missing": {k: sum(not r[k].strip() for r in rows) for k in rows[0]},
        "distinct": {k: len({r[k] for r in rows if r[k].strip()}) for k in rows[0]},
    }


def distribution(rows, key, limit=12):
    return dict(Counter(r[key] or "<missing>" for r in rows).most_common(limit))


def quantiles(values):
    values = sorted(values)
    return {str(q): values[round((len(values)-1)*q)] for q in [0, .25, .5, .75, .9, .99, 1]}


def main():
    companies = read("df_Uid_data_hack.csv")
    links = read("df_Fid_Uid_link_hack.csv")
    payments = read("df_Uid_payment_hack.csv")
    ids = {r["SYNTHETIC_U"] for r in companies}
    result = {"companies": summary(companies), "person_links": summary(links),
              "payments": summary(payments)}
    result["input_sha256"] = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in Path("data/companies").glob("*.csv")
    }
    result["companies"]["distributions"] = {
        k: distribution(companies, k) for k in companies[0]
        if k not in ["SYNTHETIC_U", "REVENUE_COMPANY", "GROUP_ID", "MAINOKVED_NAME", "CITY"]
    }
    revenues = [float(r["REVENUE_COMPANY"]) for r in companies if r["REVENUE_COMPANY"]]
    result["companies"].update({
        "duplicate_ids": len(companies)-len(ids),
        "revenue_raw_quantiles": quantiles(revenues),
        "revenue_zero": sum(v == 0 for v in revenues),
        "revenue_negative": sum(v < 0 for v in revenues),
        "revenue_above_15bn_if_unit_is_rub": sum(v > 15e9 for v in revenues),
        "fts_flag_without_country": sum(r["FTS_FLAG"] == "1.0" and not r["FTS_COUNTRY_NAME"] for r in companies),
        "client_rows_above_15bn_if_unit_is_rub": sum(
            float(r["REVENUE_COMPANY"]) > 15e9 and r["CL_TYPE"] == "CLIENT"
            for r in companies if r["REVENUE_COMPANY"]),
    })
    by_id = defaultdict(list)
    for row in companies:
        by_id[row["SYNTHETIC_U"]].append(row)
    result["companies"]["conflicting_ids"] = {
        uid: {"rows": len(group), "different_fields": [
            k for k in group[0] if len({r[k] for r in group}) > 1]}
        for uid, group in by_id.items() if len(group) > 1
    }
    group_counts = Counter(r["GROUP_ID"] for r in companies if r["GROUP_ID"])
    result["companies"]["groups_with_multiple_companies"] = sum(v > 1 for v in group_counts.values())
    person_counts = Counter(r["SYNTHETIC_F"] for r in links)
    result["person_links"].update({
        "companies_covered": len({r["SYNTHETIC_U"] for r in links} & ids),
        "unknown_company_rows": sum(r["SYNTHETIC_U"] not in ids for r in links),
        "people_linked_to_multiple_rows": sum(v > 1 for v in person_counts.values()),
        "duplicate_pairs": len(links)-len({(r["SYNTHETIC_F"], r["SYNTHETIC_U"]) for r in links}),
    })
    nodes = {r[k] for r in payments for k in ["PAYER", "BENEFICIARY"]}
    pairs = Counter((r["PAYER"], r["BENEFICIARY"]) for r in payments)
    amt = [float(r["PAY_AMT"]) for r in payments if r["PAY_AMT"]]
    cnt = [float(r["PAY_CNT"]) for r in payments if r["PAY_CNT"]]
    result["payments"].update({
        "unique_nodes": len(nodes), "known_nodes": len(nodes & ids),
        "unknown_nodes": len(nodes - ids), "companies_without_payments": len(ids - nodes),
        "rows_both_endpoints_known": sum(r["PAYER"] in ids and r["BENEFICIARY"] in ids for r in payments),
        "self_loop_rows": sum(r["PAYER"] == r["BENEFICIARY"] for r in payments),
        "duplicate_directed_pairs": sum(v-1 for v in pairs.values()),
        "amount_raw_quantiles": quantiles(amt), "count_quantiles": quantiles(cnt),
        "amount_nonpositive": sum(v <= 0 for v in amt),
        "count_nonpositive": sum(v <= 0 for v in cnt),
    })
    Path("data/processed").mkdir(exist_ok=True)
    Path("data/processed/data_audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps({k: {x: y for x, y in v.items() if x not in ["columns", "distributions", "missing", "distinct"]}
                      for k, v in result.items() if k != "input_sha256"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
