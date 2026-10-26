from fastapi import FastAPI
from app.routes import domains, verify, scans, auth, honeypots, schedules
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from shared.auth_migration import require_auth_schema
from app.database import engine
from shared.models import Base
import asyncio
import time
from contextlib import suppress
from app import health
from shared.monitoring import configure_logging, emit

app = FastAPI(title="AttackSurface Monitor API")

app.include_router(auth.router)
app.include_router(honeypots.router)
app.include_router(domains.router)
app.include_router(verify.router)
app.include_router(scans.router)
app.include_router(schedules.router)
app.include_router(health.router)


@app.on_event("startup")
async def on_startup():
    configure_logging()
    # Dev convenience: create tables if they don't exist. Swap for Alembic
    # migrations before this goes anywhere near production.
    try:
        async with engine.begin() as conn:
            await conn.run_sync(require_auth_schema)
            await conn.run_sync(Base.metadata.create_all)
    except Exception:
        emit('api', 'startup_failed')
        raise RuntimeError('API startup failed; inspect dependency configuration securely') from None
    app.state.health_task = asyncio.create_task(health.heartbeat())

@app.on_event('shutdown')
async def stop_health():
    task = getattr(app.state, 'health_task', None)
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@app.get("/")
async def root():
    return {"status": "ok", "service": "attacksurface-monitor-api"}


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # Pydantic errors otherwise echo invalid submitted passwords in "input".
    return JSONResponse(status_code=422, content={'detail':[
        {key:error[key] for key in ('loc','msg','type')} for error in exc.errors()
    ]}, headers={'Cache-Control':'no-store'})

@app.middleware('http')
async def private_responses(request, call_next):
    started = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        response = JSONResponse({'detail':'Internal server error'}, status_code=500)
    if not request.url.path.startswith('/health/'):
        emit('api', 'api_request', status_code=response.status_code,
             duration_ms=(time.monotonic()-started)*1000)
    response.headers['Cache-Control'] = 'no-store'
    return response
