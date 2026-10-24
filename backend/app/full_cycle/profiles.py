"""Company context comes only from the local operator-controlled manifest."""
import json
from pathlib import Path

from retriever.importers import read_interests
from retriever.models import digest
from .storage import PipelineError


def local_file(root, name):
    p = (root / name).resolve()
    if not p.is_relative_to(root.resolve()):
        raise PipelineError('profile_path_outside_workspace')
    return p


class ProfileStore:
    def __init__(self, root, workspace):
        self.root, self.workspace = Path(root), workspace

    def entries(self):
        manifest = self.root / 'manifest.json'
        if not manifest.is_file():
            return []
        data = json.loads(manifest.read_text())
        if data.get('schema_version') != 'full-cycle-companies.v1':
            raise PipelineError('unsupported_company_manifest')
        entries = data['companies']
        if len({e['company_id'] for e in entries}) != len(entries):
            raise PipelineError('duplicate_company_id')
        return entries

    def load(self, company_id, *, include_graph=False):
        entry = next((e for e in self.entries() if e['company_id'] == company_id), None)
        if entry is None:
            raise PipelineError('company_profile_missing')
        try:
            profile = json.loads(local_file(self.root, entry['profile']).read_text())
        except FileNotFoundError:
            raise PipelineError('company_profile_missing') from None
        if digest(profile) != entry['profile_sha256']:
            raise PipelineError('profile_hash_mismatch')
        try:
            interest_path = local_file(self.root, entry['interests'])
            interest_payload = json.loads(interest_path.read_text())
            interests = read_interests(interest_path)
        except FileNotFoundError:
            raise PipelineError('company_interests_missing') from None
        if digest(interest_payload) != entry['interests_sha256']:
            raise PipelineError('interests_hash_mismatch')
        if profile.get('company_id') != company_id or interests.company_id != company_id:
            raise PipelineError('company_id_mismatch')
        if profile.get('profile_version') != interests.profile_version:
            raise PipelineError('company_profile_version_mismatch')
        if include_graph and not any(i.active and i.family == 'graph' for i in interests.interests):
            raise PipelineError('graph_interests_missing')
        if not any(i.active and (include_graph or i.family != 'graph') for i in interests.interests):
            raise PipelineError('active_interests_missing')
        with self.workspace.db() as db:
            self.workspace.immutable(db, 'company_versions', (company_id, interests.profile_version), profile)
            # A version denotes the complete set including actual vectors and active flags.
            self.workspace.immutable(db, 'interest_versions', (company_id, interests.interest_set_version), interests)
        return profile, interests, entry['display']

    def companies(self):
        result = []
        for entry in self.entries():
            profile, interests, display = self.load(entry['company_id'])
            if display.get('company_id') != entry['company_id'] or display.get('profile_version') != interests.profile_version:
                raise PipelineError('company_display_identity_mismatch')
            result.append(display)
        return result
