"""Sector summaries describe payment neighbors, never the target company."""
from decimal import Decimal
from .representation_contracts import FeatureRef, SectorAggregate
from .payment_store import _amount


def build_sector_aggregates(neighborhood, *, repo) -> list[SectorAggregate]:
    result=[]
    for direction in ('incoming','outgoing'):
        groups={}
        for neighbor in getattr(neighborhood,direction):
            if neighbor.company_id in repo.conflicts or neighbor.company_id not in repo.rows:
                continue
            profile=repo.get(neighbor.company_id)
            code,name=profile['okved']['code'],profile['okved']['name']
            if not code or not name: continue
            groups.setdefault((code,name),[]).append((neighbor,profile))
        denominator=Decimal(neighborhood.coverage[direction].retained_amount_raw)
        for (code,name),items in sorted(groups.items()):
            amount=sum((Decimal(n.amount_raw) for n,p in items),Decimal(0))
            refs=[FeatureRef(p['company_id'],p['profile_version'],p['company_id']+':'+path) for n,p in items for path in ('/okved/code','/okved/name')]
            result.append(SectorAggregate(direction,code,name,len(items),_amount(amount),sum(n.payment_count for n,p in items),
                round(float(amount/denominator),12),sorted(n.company_id for n,p in items),sorted(n.edge_id for n,p in items),sorted(refs,key=lambda r:r.feature_id)))
    return result
