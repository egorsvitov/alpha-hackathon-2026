"""One-shot OpenAI-compatible transport. No retries, redirects, tools or streaming."""
from dataclasses import dataclass, field
import json
import os
from typing import Protocol
from .storage import StorageError, canonical_json


@dataclass(frozen=True)
class ProviderResponse:
    body: dict
    status_code: int = 200
    headers: dict = field(default_factory=dict)
    raw_body: str | None = None

    def payload(self):
        return {'status_code':self.status_code,'headers':self.headers,'body':self.body,'raw_body':self.raw_body}


class ProviderFailure(Exception):
    def __init__(self, code, message):
        self.code=code
        super().__init__(message)


class Provider(Protocol):
    is_live: bool
    def complete(self, request: dict) -> ProviderResponse: ...


class OfflineProvider:
    """Scripted transport for tests. Results must use an offline ProcessingSpec."""
    is_live=False

    def __init__(self, responses):
        self.responses=iter(responses)
        self.requests=[]

    def complete(self,request):
        self.requests.append(request)
        try:
            value=next(self.responses)
        except StopIteration:
            raise ProviderFailure('offline_responses_exhausted','No scripted provider response remains') from None
        if isinstance(value,BaseException):
            raise value
        return value


class OpenAIProvider:
    is_live=True

    def __init__(self,profile,*,api_key=None,transport=None):
        # No client construction or network during import or profile inspection.
        self.profile=profile
        self._key=api_key
        self._transport=transport

    def check_credentials(self):
        if not (self._key or os.environ.get('LLM_API_KEY')):
            raise StorageError('api_key_missing','Set LLM_API_KEY in the environment or local CLI .env')

    @staticmethod
    def _redact(value,key):
        if isinstance(value,str):
            return value.replace(key,'[REDACTED]')
        if isinstance(value,list):
            return [OpenAIProvider._redact(v,key) for v in value]
        if isinstance(value,dict):
            return {k:('[REDACTED]' if k.lower() in {'authorization','api_key','api-key'} else OpenAIProvider._redact(v,key)) for k,v in value.items()}
        return value

    def complete(self,request):
        import httpx
        import openai
        self.check_credentials()
        key=self._key or os.environ['LLM_API_KEY']
        kwargs=dict(request)
        extras={k:kwargs.pop(k) for k in ('thinking',) if k in kwargs}
        if extras:
            kwargs['extra_body']=extras
        timeout=httpx.Timeout(self.profile.read_timeout,connect=self.profile.connect_timeout,
            write=10.0,pool=5.0)
        # trust_env=False avoids unintended proxy settings; redirects cannot
        # carry the Authorization header to a second host.
        http_client=httpx.Client(timeout=timeout,follow_redirects=False,trust_env=False,transport=self._transport)
        client=openai.OpenAI(api_key=key,base_url=self.profile.base_url,max_retries=0,
            timeout=timeout,http_client=http_client)
        try:
            try:
                raw=client.chat.completions.with_raw_response.create(**kwargs)
                response=raw.http_response
            except openai.APIStatusError as exc:
                response=exc.response
            try:
                body=json.loads(response.text)
            except ValueError:
                body={'raw_text':response.text}
            if not isinstance(body,dict):
                body={'raw_payload':body}
            headers={k:v for k,v in response.headers.items() if k.lower() in {'x-request-id','retry-after'} or k.lower().startswith('x-ratelimit')}
            result=ProviderResponse(self._redact(body,key),response.status_code,self._redact(headers,key),self._redact(response.text,key))
            try:
                canonical_json(result.payload())
            except StorageError:
                # Preserve even a non-standard JSON body as text. It cannot be
                # treated as a valid model result or trusted token usage.
                result=ProviderResponse({'raw_text':self._redact(response.text,key)},response.status_code,self._redact(headers,key),self._redact(response.text,key))
            return result
        except openai.APITimeoutError:
            raise ProviderFailure('provider_timeout','Provider timed out; cost is uncertain') from None
        except openai.APIConnectionError:
            raise ProviderFailure('provider_transport_error','Provider connection failed; cost is uncertain') from None
        finally:
            client.close()
