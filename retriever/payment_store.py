"""Persistent, indexed directed payment evidence with exact Decimal totals."""
from __future__ import annotations
from dataclasses import replace
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from ranking.graph import POLICY
from ranking.profiles import csv_rows, decimal_string, file_hash, parse_decimal, ProfileError, resolve_data_path
from .company_views import build_company_views
from .representation_contracts import (DirectionCoverage, GraphBuildReport, Neighbor,
    PaymentDatasetManifest, PaymentNeighborhood, ViewPolicy, content_hash)


def _edge_id(payer, beneficiary):
    return content_hash({'payer_id':payer,'beneficiary_id':beneficiary,'policy':POLICY})


def _amount(value):
    # Canonical numerical spelling makes export formatting irrelevant to content.
    return format(value.normalize(), 'f') if value else '0'


class PaymentGraphStore:
    def __init__(self, path, db, metadata, statistics):
        self.path, self.db, self.dataset_manifest, self.statistics = Path(path), db, metadata, statistics

    @classmethod
    def open(cls, path):
        path=Path(path).resolve()
        db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
        try:
            metadata=dict(db.execute('SELECT key,value FROM metadata'))
            if metadata.get('schema') != 'payment-store.v1' or db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('invalid payment store')
            return cls(path,db,PaymentDatasetManifest(**json.loads(metadata['manifest'])),json.loads(metadata['statistics']))
        except BaseException:
            db.close(); raise

    def close(self):
        self.db.close()

    @classmethod
    def build(cls, csv_path, *, output_path, metadata: PaymentDatasetManifest):
        source=resolve_data_path(csv_path); output=Path(output_path)
        digest=file_hash(source)
        if metadata.source_sha256 != digest or metadata.policy_version != POLICY:
            raise ValueError('payment source hash or policy mismatch')
        if any((metadata.currency,metadata.period_start,metadata.period_end,metadata.available_at)):
            proof=metadata.metadata_source or {}
            proof_path=proof.get('path')
            if not proof_path or not proof.get('locator') or not proof.get('sha256') or file_hash(resolve_data_path(proof_path)) != proof['sha256']:
                raise ValueError('economic metadata requires verified manifest source')
        if output.exists():
            old=cls.open(output)
            try:
                if old.dataset_manifest == metadata:
                    return GraphBuildReport(str(output),statistics=old.statistics,source_sha256=digest)
            finally: old.close()
        output.parent.mkdir(parents=True,exist_ok=True)
        fd,temp=tempfile.mkstemp(prefix=output.name+'.',suffix='.tmp',dir=output.parent);os.close(fd)
        db=None
        try:
            db=sqlite3.connect(temp)
            db.execute('PRAGMA cache_size=-4096')
            db.execute('CREATE TABLE raw_edges(payer TEXT,beneficiary TEXT,record INTEGER,amount TEXT,count TEXT,valid INTEGER,self_loop INTEGER)')
            stats={'rows_read':0,'self_loops_excluded':0,'invalid_rows':0,'unassignable_records':0}
            with db:
                for record,row in csv_rows(source,{'PAYER','BENEFICIARY','PAY_AMT','PAY_CNT'}):
                    stats['rows_read']+=1
                    payer,beneficiary=row['PAYER'].strip(),row['BENEFICIARY'].strip()
                    self_loop=bool(payer and payer==beneficiary)
                    amount,count=parse_decimal(row['PAY_AMT']),parse_decimal(row['PAY_CNT'])
                    count_ok=count is not None and count>0 and count==count.to_integral_value()
                    valid=bool(payer and beneficiary and amount is not None and amount>0 and count_ok)
                    stats['self_loops_excluded']+=int(self_loop)
                    stats['unassignable_records']+=int(not payer or not beneficiary)
                    stats['invalid_rows']+=int(not valid and not self_loop)
                    db.execute('INSERT INTO raw_edges VALUES(?,?,?,?,?,?,?)',(payer,beneficiary,record,_amount(amount) if amount is not None and amount>0 else None,str(int(count)) if count_ok else None,int(valid),int(self_loop)))
                db.execute('CREATE INDEX raw_payer ON raw_edges(payer,beneficiary)')
                db.execute('CREATE INDEX raw_beneficiary ON raw_edges(beneficiary,payer)')
                db.execute('CREATE TABLE multiplicity AS SELECT payer,beneficiary,COUNT(*) AS n FROM raw_edges WHERE self_loop=0 GROUP BY payer,beneficiary')
                db.execute('CREATE UNIQUE INDEX pair_counts ON multiplicity(payer,beneficiary)')
                db.execute('CREATE TABLE retained_edges AS SELECT e.payer,e.beneficiary,e.record,e.amount,e.count FROM raw_edges e JOIN multiplicity m USING(payer,beneficiary) WHERE e.valid=1 AND e.self_loop=0 AND m.n=1')
                db.execute('CREATE UNIQUE INDEX retained_payer ON retained_edges(payer,beneficiary)')
                db.execute('CREATE UNIQUE INDEX retained_beneficiary ON retained_edges(beneficiary,payer)')
                stats['ambiguous_pair_rows_excluded']=db.execute('SELECT COALESCE(SUM(n),0) FROM multiplicity WHERE n>1').fetchone()[0]
                stats['retained_edges']=db.execute('SELECT COUNT(*) FROM retained_edges').fetchone()[0]
                if file_hash(source)!=digest: raise ProfileError('source_changed','Payment CSV changed while loading')
                db.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
                db.executemany('INSERT INTO metadata VALUES(?,?)',[('schema','payment-store.v1'),('manifest',json.dumps(metadata.to_dict(),sort_keys=True)),('statistics',json.dumps(stats,sort_keys=True))])
            db.close();db=None
            with open(temp,'rb') as stream: os.fsync(stream.fileno())
            os.replace(temp,output)
            parent_fd=os.open(output.parent,os.O_DIRECTORY)
            try: os.fsync(parent_fd)
            finally: os.close(parent_fd)
            return GraphBuildReport(str(output),statistics=stats,source_sha256=digest)
        finally:
            if db: db.close()
            if os.path.exists(temp): os.unlink(temp)

    def neighborhood(self, company_id: str, *, repo):
        neighbors={}; coverage={}; records={}; semantic_neighbors={}; unknown={}
        for direction,column in [('incoming','beneficiary'),('outgoing','payer')]:
            cleaning={'input_records':0,'retained_records':0,'excluded_ambiguous_records':0,'excluded_invalid_records':0}
            total=Decimal(0);positive=Decimal(0);all_known=True; retained=[]; local_losses=[]
            query=f'SELECT e.payer,e.beneficiary,e.record,e.amount,e.count,e.valid,m.n FROM raw_edges e JOIN multiplicity m USING(payer,beneficiary) WHERE e.{column}=? AND e.self_loop=0 ORDER BY e.payer,e.beneficiary,e.amount,e.count'
            for payer,beneficiary,record,amount,count,valid,n in self.db.execute(query,(company_id,)):
                cleaning['input_records']+=1
                if amount is None: all_known=False
                else: positive+=Decimal(amount)
                if n>1: reason='excluded_ambiguous_records'
                elif not valid: reason='excluded_invalid_records'
                else:
                    cleaning['retained_records']+=1;total+=Decimal(amount)
                    retained.append((payer,beneficiary,record,amount,int(count)));continue
                cleaning[reason]+=1
                local_losses.append((payer,beneficiary,amount,count,reason))
            has_losses=bool(local_losses)
            quality=('limited' if has_losses else 'clean_observed') if retained else 'unavailable'
            cleaning.update(retained_record_fraction=round(len(retained)/cleaning['input_records'],12) if cleaning['input_records'] else None,
                retained_amount_raw=_amount(total),valid_positive_input_amount_raw=_amount(positive),
                retained_positive_amount_fraction=round(float(total/positive),12) if all_known and positive>0 else None,share_quality=quality)
            items=[];known_amount=Decimal(0);known_count=0;known_payments=0; sectors_unknown={'neighbor_count':0,'amount_raw':'0','payment_count':0};unknown_amount=Decimal(0)
            semantics=[]
            for payer,beneficiary,record,amount,count in retained:
                other=payer if direction=='incoming' else beneficiary
                status='conflicting' if other in repo.conflicts else ('usable' if other in repo.rows else 'missing')
                business=None;profile=None
                if status=='usable':
                    profile=repo.get(other);business=build_company_views(profile,policy=ViewPolicy()).views.get('business')
                    known_count+=1;known_amount+=Decimal(amount);known_payments+=count
                edge=_edge_id(payer,beneficiary)
                items.append(Neighbor(other,direction,edge,amount,count,round(float(Decimal(amount)/total),12),status,
                    business.content_hash if business else None,profile['profile_version'] if profile else None,business.feature_refs if business else []))
                semantics.append({'payer':payer,'beneficiary':beneficiary,'amount':amount,'count':count,'profile_status':status})
                if profile is None or not profile['okved'].get('code') or not profile['okved'].get('name'):
                    sectors_unknown['neighbor_count']+=1;unknown_amount+=Decimal(amount);sectors_unknown['payment_count']+=count
            sectors_unknown['amount_raw']=_amount(unknown_amount);unknown[direction]=sectors_unknown
            neighbors[direction]=items;records[direction]=[r[2] for r in retained]
            coverage[direction]=DirectionCoverage(retained_neighbor_count=len(items),usable_profile_count=known_count,
                retained_amount_raw=_amount(total),usable_profile_amount_raw=_amount(known_amount),
                known_node_fraction=round(known_count/len(items),12) if items else None,
                known_amount_fraction=round(float(known_amount/total),12) if total else None,
                retained_payment_count=sum(r[4] for r in retained),known_payment_count=known_payments,cleaning_coverage=cleaning,share_quality=quality)
            semantic_neighbors[direction]={'edges':semantics,'cleaning':cleaning,'excluded':sorted(local_losses,key=lambda x:tuple('' if v is None else str(v) for v in x))}
        economics={k:getattr(self.dataset_manifest,k) for k in ('currency','period_start','period_end','available_at','policy_version')}
        graph_hash=content_hash({'company_id':company_id,'directions':semantic_neighbors,'economics':economics})
        provenance=content_hash({'manifest':self.dataset_manifest,'records':records})
        limitations=['payer_payee_not_proven_supplier_customer_roles','aggregated_payments_not_total_company_turnover']
        if self.dataset_manifest.currency is None: limitations.append('currency_unknown')
        if self.dataset_manifest.period_start is None or self.dataset_manifest.period_end is None: limitations.append('period_unknown')
        if self.dataset_manifest.available_at is None: limitations.append('available_at_unknown')
        return PaymentNeighborhood(company_id,self.dataset_manifest,graph_hash,provenance,neighbors['incoming'],neighbors['outgoing'],coverage,limitations,unknown)

    def affected_companies(self, changed_company_ids: set[str]) -> set[str]:
        affected=set()
        for uid in sorted(changed_company_ids):
            affected.update(row[0] for row in self.db.execute('SELECT beneficiary FROM retained_edges WHERE payer=?',(uid,)))
            affected.update(row[0] for row in self.db.execute('SELECT payer FROM retained_edges WHERE beneficiary=?',(uid,)))
        return affected
