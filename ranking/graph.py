"""Optional streaming aggregation of the supplied undated payment sample."""
import copy
import csv
from decimal import Decimal, InvalidOperation
import hashlib
from pathlib import Path
from .profiles import ROOT

DEFAULT_PAYMENTS = ROOT / "data/companies/df_Uid_payment_hack.csv"


def attach_payment_graph(profiles, known_company_ids, path=DEFAULT_PAYMENTS):
    path = Path(path)
    if not path.exists():
        raise ValueError(f"Нет платежного CSV: {path}")
    wanted = {p["company_id"] for p in profiles}
    selected, duplicates, pair_counts = {}, set(), {}
    stats = {"rows_read": 0, "selected_rows": 0, "self_loops_excluded": 0,
             "invalid_selected_rows": 0, "ambiguous_pair_rows_excluded": 0}
    # Keep only edges touching requested companies, never all 1.38M row dicts.
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, delimiter=";")
        if not {"PAYER", "BENEFICIARY", "PAY_AMT", "PAY_CNT"} <= set(reader.fieldnames or []):
            raise ValueError("В платежном CSV отсутствуют обязательные поля")
        for record, row in enumerate(reader, 1):
            stats["rows_read"] += 1
            payer, beneficiary = row["PAYER"], row["BENEFICIARY"]
            if payer not in wanted and beneficiary not in wanted:
                continue
            stats["selected_rows"] += 1
            if payer == beneficiary:
                stats["self_loops_excluded"] += 1
                continue
            pair = (payer, beneficiary)
            pair_counts[pair] = pair_counts.get(pair, 0)+1
            if pair_counts[pair] > 1:
                duplicates.add(pair)
            try:
                amount = Decimal(row["PAY_AMT"])
                count = int(row["PAY_CNT"])
                if not amount.is_finite() or amount <= 0 or count <= 0:
                    raise ValueError("nonpositive")
            except (InvalidOperation, ValueError, TypeError):
                stats["invalid_selected_rows"] += 1
                continue
            if pair in selected:
                duplicates.add(pair)
                selected[pair][3] += 1
            else:
                selected[pair] = [amount, count, record, 1]
    for pair in duplicates:
        stats["ambiguous_pair_rows_excluded"] += pair_counts[pair]
        selected.pop(pair, None)
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024*1024), b""):
            hasher.update(block)
    try:
        source_path = str(path.resolve().relative_to(ROOT))
    except ValueError:
        source_path = str(path.resolve())
    result = copy.deepcopy(profiles)
    for profile in result:
        uid, neighbors = profile["company_id"], []
        for direction in ["incoming", "outgoing"]:
            edges = [(pair, values) for pair, values in selected.items()
                     if pair[1 if direction == "incoming" else 0] == uid]
            total = sum((values[0] for _, values in edges), Decimal(0))
            for pair, (amount, count, record, _) in edges:
                other = pair[0 if direction == "incoming" else 1]
                share = float(amount/total) if total else 0
                path_key = f"graph.{direction}.{other}"
                fid = f"{uid}:{path_key}"
                feature = {"feature_id": fid, "path": path_key, "value": share,
                           "quality": "derived", "source": {"file": source_path,
                           "file_sha256": hasher.hexdigest(), "data_record_number": record,
                           "columns": ["PAYER", "BENEFICIARY", "PAY_AMT"],
                           "aggregation": "amount / observed direction total, excluding self loops and ambiguous pairs"}}
                profile["features"][fid] = feature
                neighbors.append({"company_id": other, "direction": direction,
                                  "observed_amount_raw": str(amount), "payment_count": count,
                                  "observed_share": share, "profile_known": other in known_company_ids,
                                  "feature_id": fid})
        neighbors.sort(key=lambda n: (-n["observed_share"], n["direction"], n["company_id"]))
        distinct_neighbors = {n["company_id"] for n in neighbors}
        profile["graph"] = {"status": "loaded", "period": None, "currency": None,
                            "neighbors": neighbors,
                            "profile_coverage_by_nodes": (len(distinct_neighbors & set(known_company_ids))/len(distinct_neighbors)
                                                          if distinct_neighbors else None),
                            "limitations": ["undated_sample_not_total_company_turnover",
                                            "payer_and_payee_are_not_proven_supplier_customer_roles",
                                            "all_repeated_directed_pairs_excluded_due_to_unknown_semantics"]}
        profile["profile_version"] += f":graph-v1:{hasher.hexdigest()[:12]}"
    return result, stats
