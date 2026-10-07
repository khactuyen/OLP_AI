import asyncio
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .context_budget import BudgetError, DeepSeekCounter, build_budget
from .cloudflare_client import Cloudflare
from .openrouter_client import MODEL, DemoRouter, OpenRouter, ProviderError
from .storage import Store, new_id

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / 'static'


@dataclass
class Settings:
    data_dir: Path = ROOT / 'data'
    key: str = ''
    provider: str = ''
    limit: int = 2048
    margin: int = 64
    output_target: int = 768
    temperature: float = 0.6
    demo: bool = False
    backend: str = 'cloudflare'
    account_id: str = ''

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / '.env', override=False)
        backend = os.getenv('OLP_BACKEND', 'cloudflare').strip().lower()
        if backend not in ('cloudflare', 'openrouter'):
            raise ValueError('OLP_BACKEND phải là cloudflare hoặc openrouter.')
        config = cls(backend=backend,
                     account_id=os.getenv('CLOUDFLARE_ACCOUNT_ID', '').strip(),
                     key=os.getenv('CLOUDFLARE_API_TOKEN' if backend == 'cloudflare' else 'OPENROUTER_API_KEY', '').strip(),
                     provider=os.getenv('OPENROUTER_PROVIDER', '').strip(),
                     limit=int(os.getenv('OLP_CONTEXT_LIMIT', '2048')),
                     margin=int(os.getenv('OLP_TOKEN_MARGIN', '64')),
                     output_target=int(os.getenv('OLP_OUTPUT_TARGET', '768')),
                     temperature=float(os.getenv('OLP_TEMPERATURE', '0.6')),
                     demo=os.getenv('OLP_DEMO') == '1')
        if config.limit not in (2000, 2048) or not 32 <= config.margin <= 256:
            raise ValueError('Context phải là 2000 hoặc 2048; margin phải từ 32 đến 256.')
        if not 128 <= config.output_target <= 1024 or not 0 <= config.temperature <= 2:
            raise ValueError('Output target phải từ 128 đến 1024; temperature từ 0 đến 2.')
        return config


class SessionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    policy: str = 'new_session'


class ChatInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = Field(min_length=1, max_length=20000)


def frame(event, data):
    return f'event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'


def create_app(settings=None, counter=None, router=None, now=None):
    settings = settings or Settings.from_env()
    now = now or time.time
    app = FastAPI(title='OlpAI Practice Chat', docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', 'testserver'])
    store = Store(settings.data_dir / ('demo.sqlite3' if settings.demo else 'practice.sqlite3'))
    if settings.backend == 'cloudflare' and not settings.demo:
        store.recover_reasoning_template_blocks(settings.limit)
    tokenizer_error = None
    if counter is None:
        try:
            counter = DeepSeekCounter(settings.data_dir / 'tokenizer')
        except (OSError, ValueError, KeyError):
            tokenizer_error = 'Tokenizer chưa sẵn sàng. Chạy prepare_tokenizer.py trước khi luyện.'
    router = router or (DemoRouter() if settings.demo else
                        Cloudflare(settings.key, settings.account_id, settings.temperature) if settings.backend == 'cloudflare' else
                        OpenRouter(settings.key, settings.provider, settings.temperature))
    active_model = getattr(router, 'model', MODEL)
    provider_label = 'Cloudflare Workers AI' if settings.backend == 'cloudflare' else 'OpenRouter'
    configuration_error = None
    if not settings.demo:
        if hasattr(router, 'configuration_error'):
            configuration_error = router.configuration_error()
        elif not settings.key:
            configuration_error = 'Điền OPENROUTER_API_KEY trong .env rồi khởi động lại app.'
    locks = {}
    cancellations = {}
    availability = {'available': False, 'endpoint_count': 0, 'checked_at': None, 'error': None}
    app.state.store = store
    app.state.router = router
    app.state.settings = settings

    @app.middleware('http')
    async def local_boundary(request: Request, call_next):
        if request.method not in ('GET', 'HEAD'):
            if request.headers.get('x-local-client') != 'olpai':
                return JSONResponse({'detail': 'Chỉ chấp nhận thao tác từ ứng dụng local.'}, status_code=403)
            origin = request.headers.get('origin')
            if origin and origin != f'{request.url.scheme}://{request.headers.get("host")}':
                return JSONResponse({'detail': 'Origin không hợp lệ.'}, status_code=403)
        response = await call_next(request)
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Cache-Control'] = 'no-store'
        return response

    def policy_valid(policy):
        if policy not in ('new_session', 'sliding_window'):
            raise HTTPException(400, 'Chính sách context không hợp lệ.')

    def active_exam():
        return store.latest_exam()

    def exam_view(exam):
        if not exam:
            return None
        elapsed = max(0, now() - exam['started'])
        return {**exam, 'elapsed': elapsed, 'ai_remaining': max(0, 18000 - elapsed),
                'exam_remaining': max(0, 21600 - elapsed), 'ai_locked': elapsed >= 18000,
                'ended': elapsed >= 21600}

    def get_session(session_id):
        session = store.session(session_id)
        if not session:
            raise HTTPException(404, 'Không tìm thấy phiên.')
        return session

    def assert_exam_access(session):
        current = active_exam()
        if current and session['exam_id'] != current['id']:
            raise HTTPException(403, 'Đang thi thử: chỉ được chat trong phiên thuộc bài thi này.')
        if session['exam_id']:
            exam = store.one('SELECT * FROM exams WHERE id=?', (session['exam_id'],))
            if not exam or exam['state'] != 'active' or now() >= exam['started'] + 18000:
                raise HTTPException(403, 'Đã hết thời gian dùng AI của bài thi thử.')

    async def check_availability(force=False):
        if not force and availability['checked_at'] and now() - availability['checked_at'] < 60:
            return availability
        try:
            result = await router.availability()
            availability.update(result, checked_at=now(), error=None)
        except ProviderError as exc:
            availability.update(available=False, endpoint_count=0, checked_at=now(), error=str(exc))
        except Exception:
            availability.update(available=False, endpoint_count=0, checked_at=now(),
                                error=f'Không kiểm tra được {provider_label}. Kiểm tra kết nối rồi thử lại.')
        return availability

    @app.get('/api/status')
    async def status():
        return {'model': active_model, 'demo': settings.demo, 'key_configured': bool(settings.key),
                'backend': settings.backend, 'provider_label': provider_label,
                'configuration_error': configuration_error,
                'tokenizer_ready': counter is not None, 'tokenizer_error': tokenizer_error,
                'tokenizer_revision': getattr(counter, 'revision', None), 'context_limit': settings.limit,
                'output_target': settings.output_target, 'token_margin': settings.margin,
                'provider': settings.provider or None, 'availability': availability,
                'exam': exam_view(active_exam()), 'server_time': now(),
                'tools_enabled': False, 'context_verified': False}

    @app.post('/api/availability')
    async def refresh_availability():
        return await check_availability(force=True)

    @app.get('/api/sessions')
    async def sessions():
        return store.all('SELECT * FROM sessions ORDER BY created DESC')

    @app.post('/api/sessions')
    async def new_session(body: SessionInput):
        policy_valid(body.policy)
        exam = active_exam()
        if exam and now() >= exam['started'] + 18000:
            raise HTTPException(403, 'AI đã khóa cho bài thi này. Phiên mới không đặt lại thời gian.')
        session_id = new_id()
        store.execute('INSERT INTO sessions(id,title,created,policy,exam_id) VALUES(?,?,?,?,?)',
                      (session_id, 'Phiên mới', now(), exam['policy'] if exam else body.policy, exam['id'] if exam else None))
        return store.session(session_id)

    @app.get('/api/sessions/{session_id}')
    async def session_detail(session_id: str):
        session = get_session(session_id)
        history = store.history(session_id)
        used = counter.count(history) if counter else None
        return {**session, 'messages': store.messages(session_id), 'context_tokens': used,
                'running': store.one("SELECT id FROM generations WHERE session_id=? AND status='running'", (session_id,)),
                'generations': store.all('SELECT * FROM generations WHERE session_id=? ORDER BY created', (session_id,))}

    @app.post('/api/sessions/{session_id}/budget')
    async def preview_budget(session_id: str, body: ChatInput):
        session = get_session(session_id)
        if counter is None:
            raise HTTPException(503, tokenizer_error)
        try:
            budget = build_budget(counter, store.history(session_id), body.text, session['policy'],
                                  settings.limit, settings.margin, settings.output_target)
            return {'prompt_tokens': budget.prompt_tokens, 'max_tokens': budget.max_tokens,
                    'dropped_messages': budget.dropped_messages, 'fits': True}
        except BudgetError as exc:
            return {'fits': False, 'error': str(exc)}

    @app.post('/api/exams')
    async def start_exam(body: SessionInput):
        policy_valid(body.policy)
        if active_exam():
            raise HTTPException(409, 'Bài thi thử đang tồn tại. Kết thúc bài này trước khi bắt đầu bài khác.')
        if store.one("SELECT id FROM generations WHERE status='running'"):
            raise HTTPException(409, 'Dừng câu trả lời đang sinh trước khi bắt đầu thi thử.')
        exam_id = new_id()
        store.execute('INSERT INTO exams(id,started,policy) VALUES(?,?,?)', (exam_id, now(), body.policy))
        return exam_view(active_exam())

    @app.post('/api/exams/end')
    async def end_exam():
        exam = active_exam()
        if not exam:
            raise HTTPException(409, 'Không có bài thi thử đang hoạt động.')
        store.execute("UPDATE exams SET state='ended' WHERE id=?", (exam['id'],))
        for session_id, event in list(cancellations.items()):
            if get_session(session_id)['exam_id'] == exam['id']:
                event.set()
        return {'ended': True}

    @app.post('/api/sessions/{session_id}/stop')
    async def stop(session_id: str):
        get_session(session_id)
        event = cancellations.get(session_id)
        if event:
            event.set()
        return {'stopping': bool(event)}

    @app.get('/api/sessions/{session_id}/export')
    async def export(session_id: str):
        session = get_session(session_id)
        content = {'session': session, 'model': active_model, 'backend': settings.backend, 'demo': settings.demo,
                   'context_limit': settings.limit, 'tools_enabled': False,
                   'messages': store.messages(session_id),
                   'generations': store.all('SELECT * FROM generations WHERE session_id=? ORDER BY created', (session_id,))}
        return JSONResponse(content, headers={'Content-Disposition': f'attachment; filename="olpai-{session_id[:8]}.json"'})

    @app.post('/api/sessions/{session_id}/chat')
    async def chat(session_id: str, body: ChatInput):
        session = get_session(session_id)
        assert_exam_access(session)
        if session['blocked']:
            raise HTTPException(409, session['blocked'])
        if counter is None:
            raise HTTPException(503, tokenizer_error)
        if configuration_error:
            raise HTTPException(503, configuration_error)
        text = body.text.strip()
        if not text:
            raise HTTPException(400, 'Nhập câu hỏi trước khi gửi.')
        lock = locks.setdefault(session_id, asyncio.Lock())
        if lock.locked():
            raise HTTPException(409, 'Phiên đang có câu trả lời được sinh.')
        await lock.acquire()
        try:
            assert_exam_access(session)
            budget = build_budget(counter, store.history(session_id), text, session['policy'],
                                  settings.limit, settings.margin, settings.output_target)
            current_availability = await check_availability()
            if not current_availability['available']:
                raise HTTPException(503, current_availability['error'] or f'{provider_label} chưa có endpoint cho đúng model 32B. Không tự đổi model.')
            # Availability checking may cross the deadline.
            assert_exam_access(session)
            generation_id = store.reserve(session, budget, text, 'demo' if settings.demo else settings.backend, active_model)
        except BudgetError as exc:
            lock.release()
            raise HTTPException(409, str(exc)) from None
        except sqlite3.IntegrityError:
            lock.release()
            raise HTTPException(409, 'Phiên đang xử lý một request khác.') from None
        except BaseException:
            lock.release()
            raise
        event = asyncio.Event()
        cancellations[session_id] = event

        async def generate():
            content, reasoning, usage, finish_reason, upstream_id = '', '', None, None, None
            outcome, error = 'error', None
            pending = None
            stop_wait = None
            deadline = now() + 180
            if session['exam_id']:
                exam = store.one('SELECT * FROM exams WHERE id=?', (session['exam_id'],))
                deadline = min(deadline, exam['started'] + 18000)
            stream = router.stream(budget).__aiter__()
            try:
                yield frame('start', {'generation_id': generation_id, 'prompt_tokens': budget.prompt_tokens,
                                      'max_tokens': budget.max_tokens, 'dropped_messages': budget.dropped_messages})
                stop_wait = asyncio.create_task(event.wait())
                while True:
                    remaining = deadline - now()
                    if remaining <= 0:
                        error = 'Đã hết giờ dùng AI.' if session['exam_id'] and now() >= exam['started'] + 18000 else 'Hết thời gian chờ câu trả lời.'
                        outcome = 'cancelled'
                        break
                    pending = asyncio.create_task(anext(stream))
                    done, _ = await asyncio.wait({pending, stop_wait}, timeout=remaining,
                                                 return_when=asyncio.FIRST_COMPLETED)
                    if stop_wait in done or not done or now() >= deadline:
                        outcome = 'cancelled'
                        error = 'Câu trả lời đã dừng.' if done else 'Đã hết thời gian sinh câu trả lời.'
                        break
                    try:
                        chunk = pending.result()
                    except StopAsyncIteration:
                        if finish_reason not in ('stop', 'length'):
                            raise ProviderError('Stream kết thúc chưa có trạng thái hoàn tất.')
                        outcome = 'complete'
                        break
                    kind = chunk['type']
                    if kind == 'content':
                        content += chunk['text']
                        yield frame('delta', {'text': chunk['text']})
                    elif kind == 'reasoning':
                        reasoning += chunk['text']
                        yield frame('reasoning', {'text': chunk['text']})
                    elif kind == 'usage':
                        usage = chunk['value']
                    elif kind == 'finish':
                        finish_reason = chunk['value']
                    elif kind == 'id':
                        upstream_id = chunk['value']
                if not settings.demo and outcome == 'complete':
                    valid_usage = usage and isinstance(usage.get('prompt_tokens'), int) and isinstance(usage.get('completion_tokens'), int)
                    # Always fail closed on absent accounting. The app cannot
                    # verify the hard cap without the provider's measured usage.
                    if not valid_usage:
                        error = 'Provider chưa trả usage để xác minh context. Phiên đã khóa; không tự gọi lại.'
                        store.execute('UPDATE sessions SET blocked=? WHERE id=?', (error, session_id))
                    elif usage['prompt_tokens'] + usage['completion_tokens'] > settings.limit:
                        error = 'Usage thực vượt giới hạn context. Phiên đã khóa; cần hiệu chỉnh tokenizer/provider.'
                        store.execute('UPDATE sessions SET blocked=? WHERE id=?', (error, session_id))
                    elif usage['prompt_tokens'] > budget.prompt_tokens + settings.margin:
                        error = 'Sai lệch template vượt dự phòng. Phiên đã khóa để kiểm tra.'
                        store.execute('UPDATE sessions SET blocked=? WHERE id=?', (error, session_id))
                if error:
                    yield frame('warning' if outcome == 'complete' else 'error', {'message': error})
            except asyncio.CancelledError:
                outcome, error = 'cancelled', 'Kết nối đã đóng; câu trả lời có thể chưa hoàn chỉnh.'
                raise
            except Exception as exc:
                outcome = 'error'
                # Only our own sanitized errors are shown; never dump HTTP
                # request headers or SDK exceptions containing credentials.
                error = str(exc) if isinstance(exc, ProviderError) else 'Không nhận được câu trả lời từ provider. Kiểm tra kết nối rồi thử lại.'
                yield frame('error', {'message': error})
            finally:
                for task in (pending, stop_wait):
                    if task and not task.done():
                        task.cancel()
                for task in (pending, stop_wait):
                    if task:
                        try:
                            await task
                        except (asyncio.CancelledError, StopAsyncIteration, Exception):
                            pass
                try:
                    await stream.aclose()
                finally:
                    store.finish(session_id, generation_id, budget, content, reasoning, outcome,
                                 usage, finish_reason, upstream_id, error)
                    cancellations.pop(session_id, None)
                    lock.release()
            yield frame('done', {'status': outcome, 'usage': usage, 'finish_reason': finish_reason,
                                 'demo': settings.demo, 'warning': error})

        return StreamingResponse(generate(), media_type='text/event-stream',
                                 headers={'X-Accel-Buffering': 'no'})

    app.mount('/static', StaticFiles(directory=STATIC), name='static')

    @app.get('/')
    async def home():
        return FileResponse(STATIC / 'index.html')

    return app
