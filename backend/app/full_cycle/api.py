"""Read-only saved-result facade; HTTP reads never invoke models."""
import os

from app.schemas.base import CompanyProfile, FeedItem, Insight, QaAnswer
from .profiles import ProfileStore
from .storage import PipelineError, Workspace

MESSAGES = {
    'company_profile_missing':'Профиль компании ещё не загружен.',
    'company_interests_missing':'Интересы компании ещё не загружены.',
    'index_not_ready':'Индекс новостей ещё не готов.',
    'job_not_started':'Поиск и анализ для этой компании и роли ещё не запускались.',
    'card_not_found':'Сохранённая карточка для этой компании и роли не найдена.',
    'upload_requires_worker':'Обработка загруженных новостей доступна через CLI worker.',
    'budget_exhausted':'Анализ остановлен: исчерпан общий лимит вызовов.',
    'analysis_failed':'Анализ не выполнен; требуется проверка задания.',
}


def enabled():
    return os.environ.get('FULL_CYCLE_MODE','demo') == 'persisted'


class SavedDataStore:
    def __init__(self, root=None, profile_root=None):
        self.workspace = Workspace(root or os.environ.get('FULL_CYCLE_DATA','/data/full-cycle'))
        self.profiles = ProfileStore(profile_root or os.environ.get('FULL_CYCLE_PROFILES',str(self.workspace.root/'companies')),self.workspace)

    def get_companies(self):
        return [CompanyProfile.model_validate(c) for c in self.profiles.companies()]

    def processing_state(self,company_id,role):
        self.profiles.load(company_id)
        job = self.workspace.latest(company_id,role)
        if not job:
            return dict(status='not_started',code='job_not_started',message=MESSAGES['job_not_started'],counts={})
        index = self.workspace.state('index')
        ingestion = self.workspace.state('ingestion')
        stale = not index or job.get('index_identity') != index['identity']
        profile, interests, _ = self.profiles.load(company_id)
        stale = stale or job.get('profile_version') != interests.profile_version or job.get('interest_set_version') != interests.interest_set_version
        status = 'stale' if stale and job['status'] != 'failed' else job['status']
        labels = {'ready':'Анализ завершён. Показаны сохранённые результаты.',
                  'partial':'Анализ завершён частично. Часть событий ожидает обработки.',
                  'running':'Задание выполняется.', 'failed':'Задание завершилось с ошибкой.',
                  'stale':'Корпус или профиль обновлён. Требуется повторный анализ.'}
        return dict(status=status,message=labels.get(status,status),job_id=job['id'],counts=job.get('counts',{}),
            error_code=job.get('error_code'),as_of=job['as_of'],profile_version=job.get('profile_version'),
            interest_set_version=job.get('interest_set_version'), limitations=job.get('batch',{}).get('limitations',[]),
            ingestion=ingestion and {k:ingestion.get(k) for k in ('status','code','last_good_index')})

    def get_feed(self,company_id,role):
        state = self.processing_state(company_id,role)
        if state['status'] == 'not_started':
            return []
        job = self.workspace.latest(company_id,role)
        if state['status'] == 'stale':
            return []
        return sorted([FeedItem.model_validate(c['feed']) for c in self.workspace.cards_for(job['id'])],
                      key=lambda x:(-x.relevance.score,x.event.event_id))

    def get_insight(self,event_id,company_id,role):
        self.profiles.load(company_id)
        # Historical cards remain addressable by their immutable card ID.
        with self.workspace.db() as db:
            row = db.execute('SELECT payload FROM cards WHERE id=? AND company_id=? AND role=?', (event_id,company_id,role)).fetchone()
        if not row:
            raise PipelineError('card_not_found')
        import json
        return Insight.model_validate(json.loads(row[0])['insight'])

    def ask(self,**kwargs):
        self.get_insight(kwargs['news_id'],kwargs['company_id'],kwargs['role'])
        return QaAnswer(text='Ответы на дополнительные вопросы в полном цикле пока не подключены. Используйте источники сохранённой карточки.',insufficient=True)

    def ingest_news(self,**kwargs):
        raise PipelineError('upload_requires_worker')

    def get_metrics(self):
        return self.readiness()

    def readiness(self):
        index = self.workspace.state('index')
        return dict(status='ready' if index else 'index_not_ready',mode='persisted',index_ready=bool(index),
                    ingestion=self.workspace.state('ingestion'),worker=self.workspace.state('worker'))


def current_store():
    return SavedDataStore()
