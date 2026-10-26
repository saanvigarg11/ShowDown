"""Allowlisted operational telemetry: never serialize messages, targets or exceptions."""
import asyncio
import json
import logging
import math
import os
import sys
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, func, text
from shared.models import Scan, ScanStatus, ScanSchedule, ScanNotification
from shared.time_utils import database_utc

EVENTS = {'api_request', 'api_health', 'worker_health', 'worker_failure', 'scan_started',
          'scan_completed', 'scan_failed', 'invalid_job', 'scheduler_started',
          'scheduler_completed', 'scheduler_failed', 'scheduler_deferred',
          'notification_result', 'notification_failure', 'startup_failed', 'runtime_log'}
NUMBERS = {'status_code', 'duration_ms', 'attempt', 'database_ok', 'redis_ok', 'healthy',
           'completed_count', 'failed_count', 'queued_count', 'running_count',
           'overdue_schedules', 'notification_failures', 'notification_stuck',
           'notification_pending', 'notification_overdue', 'old_queued_count',
           'oldest_running_seconds', 'scan_duration_seconds', 'loop_ok'}
STATES = {'sent', 'pending', 'failed', 'uncertain', 'expired', 'cancelled', 'suppressed', 'disabled'}

def number_env(name, default, minimum=1, maximum=86400):
    try:
        return max(minimum, min(maximum, int(os.environ.get(name, default))))
    except ValueError:
        return default

def emit(component, event, **fields):
    if component not in {'api', 'worker', 'scheduler', 'notifications', 'runtime'} or event not in EVENTS:
        return
    payload = {'schema': 'showdown.monitor.v1', 'time': datetime.now(timezone.utc).isoformat(),
               'component': component, 'event': event}
    for key, value in fields.items():
        if key in NUMBERS and isinstance(value, (int, float)) and math.isfinite(value):
            payload[key] = round(value, 3)
        elif key == 'state' and isinstance(value, str) and value in STATES:
            payload[key] = value
    try:
        print(json.dumps(payload, separators=(',', ':')), file=sys.stdout, flush=True)
    except (OSError, ValueError):
        pass  # Logging failure must not change scan/business transactions.

class SafeFormatter(logging.Formatter):
    def format(self, record):
        # Library messages/tracebacks may contain URLs, SQL parameters or tokens.
        return json.dumps({'schema':'showdown.monitor.v1', 'component':'runtime',
            'event':'runtime_log', 'level':record.levelname})

def configure_logging():
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(SafeFormatter())
    logging.getLogger().handlers = [handler]
    logging.getLogger().setLevel(logging.WARNING)
    for name in ('uvicorn', 'uvicorn.error', 'uvicorn.access', 'httpx', 'httpcore', 'sqlalchemy'):
        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.WARNING)
    logging.getLogger('uvicorn.access').disabled = True

async def dependencies(sessions, redis):
    async def database():
        async with sessions() as db:
            await db.execute(text('SELECT 1'))
    async def check(call):
        try:
            await asyncio.wait_for(call(), timeout=3)
            return 1
        except Exception:
            return 0
    db_ok, redis_ok = await asyncio.gather(check(database), check(redis.ping))
    return {'database_ok':db_ok, 'redis_ok':redis_ok, 'healthy':int(bool(db_ok and redis_ok))}

async def snapshot(sessions, now=None):
    """Aggregate existing rows only. No schema changes or target/owner identifiers."""
    now = database_utc(now)
    window = now - timedelta(minutes=15)
    grace = timedelta(seconds=number_env('MONITOR_SCHEDULE_GRACE_SECONDS', 900))
    async with sessions() as db:
        async def count(model, *conditions):
            return await db.scalar(select(func.count()).select_from(model).where(*conditions))
        counts = {}
        for status in (ScanStatus.queued, ScanStatus.running):
            counts[status.value+'_count'] = await count(Scan, Scan.status == status)
        for status in (ScanStatus.completed, ScanStatus.failed):
            counts[status.value+'_count'] = await count(Scan, Scan.status == status, Scan.completed_at >= window)
        counts['old_queued_count'] = await count(Scan, Scan.status == ScanStatus.queued, Scan.created_at < now-grace)
        counts['overdue_schedules'] = await count(ScanSchedule, ScanSchedule.enabled.is_(True), ScanSchedule.next_run_at < now-grace)
        counts['notification_failures'] = await count(ScanNotification,
            ScanNotification.status.in_(['failed', 'uncertain', 'expired']),
            ScanNotification.created_at >= now-timedelta(days=1))
        counts['notification_stuck'] = await count(ScanNotification, ScanNotification.status == 'sending',
            ScanNotification.last_attempt_at < now-timedelta(minutes=5))
        counts['notification_pending'] = await count(ScanNotification, ScanNotification.status == 'pending')
        counts['notification_overdue'] = await count(ScanNotification, ScanNotification.status == 'pending',
            ScanNotification.next_attempt_at < now-grace)
        oldest = await db.scalar(select(func.min(Scan.created_at)).where(Scan.status == ScanStatus.running))
        counts['oldest_running_seconds'] = max(0, (now-oldest).total_seconds()) if oldest else 0
        if db.get_bind().dialect.name == 'sqlite':
            duration = (func.julianday(Scan.completed_at)-func.julianday(Scan.created_at))*86400
        else:
            duration = func.extract('epoch', Scan.completed_at-Scan.created_at)
        counts['scan_duration_seconds'] = max(0, (await db.scalar(select(func.max(duration)).where(
            Scan.completed_at >= window, Scan.status.in_([ScanStatus.completed, ScanStatus.failed])))) or 0)
        return counts
