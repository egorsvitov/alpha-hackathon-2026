"""Argparse hook used by the shared ranking command."""
import json
import sys
from pathlib import Path
from .profiles import CompanyRepository, DEFAULT_CONFIG, ProfileError, resolve_data_path
from .graph import attach_payment_graph
from .contracts import require_valid_contract

def register_parser(subparsers):
    p = subparsers.add_parser('profile',help='Normalize one company CSV snapshot')
    p.add_argument('--company-id',required=True)
    p.add_argument('--companies-csv',type=Path,required=True)
    p.add_argument('--normalization-config',type=Path,default=DEFAULT_CONFIG)
    p.add_argument('--payments-csv',type=Path)
    p.add_argument('--with-payments',action='store_true')
    p.add_argument('--source-manifest',type=Path)
    p.add_argument('--synthetic',action='store_true',help='Explicitly declare that the entire source is fictional')
    p.add_argument('--state-dir',type=Path,default=Path('data/processed/personalization'))
    p.add_argument('--output',type=Path)
    p.set_defaults(func=run)
    return p

def run(args):
    try:
        repo = CompanyRepository(args.companies_csv,normalization_config=args.normalization_config,source_manifest=args.source_manifest,is_synthetic=args.synthetic)
        profile = repo.get(args.company_id)
        if args.with_payments:
            if args.payments_csv is None:
                raise ProfileError('payments_csv_required','--with-payments requires --payments-csv')
            profile = attach_payment_graph([profile],repo.company_ids,args.payments_csv,config={'state_dir':args.state_dir})[0][0]
        require_valid_contract(profile,'company-profile')
        encoded = json.dumps(profile,ensure_ascii=False,indent=2,allow_nan=False)+'\n'
        if args.output:
            output = resolve_data_path(args.output)
            output.parent.mkdir(parents=True,exist_ok=True)
            output.write_text(encoded,encoding='utf-8')
        else:
            sys.stdout.write(encoded)
        return 0
    except (ProfileError, OSError) as exc:
        print(str(exc),file=sys.stderr)
        return 2
