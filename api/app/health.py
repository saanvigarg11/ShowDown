import asyncio
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from app.database import SessionLocal
from app.routes.scans import redis_client
from shared.monitoring import dependencies, emit

router = APIRouter(tags=['health'])

@router.get('/health/live', include_in_schema=False)
async def live():
    return {'status':'ok'}

@router.get('/health/ready', include_in_schema=False)
async def ready():
    state = await dependencies(SessionLocal, redis_client)
    return JSONResponse({'status':'ok' if state['healthy'] else 'unavailable'},
                        status_code=200 if state['healthy'] else 503)

async def heartbeat():
    while True:
        emit('api', 'api_health', **await dependencies(SessionLocal, redis_client))
        await asyncio.sleep(60)
