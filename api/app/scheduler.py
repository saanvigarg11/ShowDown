"""One Azure scheduled-job tick. PostgreSQL is the existing durable scan outbox."""
import asyncio
from datetime import timedelta
from sqlalchemy import select
from shared.models import Domain, User, Scan, ScanStatus, ScanSchedule
from shared.targets import normalize_domain, ownership_matches
from shared.time_utils import database_utc
from shared.monitoring import emit, configure_logging

async def dispatch_one(db, domain_id, now):
    # Same lock order as owner API/manual scans. A retry rechecks the committed
    # due date; overlapping Azure executions cannot enqueue the same occurrence.
    domain = (await db.execute(select(Domain).where(Domain.id == domain_id)
        .with_for_update())).scalar_one_or_none()
    schedule = await db.get(ScanSchedule, domain_id)
    if not domain or not schedule or not schedule.enabled or schedule.next_run_at > now:
        return
    schedule.last_checked_at = now
    owner = await db.get(User, schedule.owner_id)
    if not owner or not owner.active or domain.owner_id != schedule.owner_id or not domain.verified:
        schedule.enabled = False
        schedule.last_outcome = 'disabled: ownership or verification unavailable'
        await db.commit()
        return
    try:
        name = normalize_domain(domain.name)
    except ValueError:
        schedule.enabled = False
        schedule.last_outcome = 'disabled: invalid target'
        await db.commit()
        return
    try:
        authorized = await asyncio.wait_for(ownership_matches(name, domain.verification_token), timeout=10)
    except Exception:
        emit('scheduler', 'scheduler_deferred')
        schedule.last_outcome = 'DNS unavailable; retry in 5 minutes'
        schedule.next_run_at = now + timedelta(minutes=5)
        await db.commit()
        return
    if not authorized:
        schedule.enabled = False
        domain.verified = False
        schedule.last_outcome = 'disabled: ownership TXT mismatch'
        await db.commit()
        return
    active = (await db.execute(select(Scan.id).where(Scan.domain_id == domain.id,
        Scan.status.in_([ScanStatus.queued, ScanStatus.running])).limit(1))).scalar_one_or_none()
    if active:
        schedule.last_outcome = 'skipped: scan already active'
    else:
        scan = Scan(domain_id=domain.id, status=ScanStatus.queued)
        db.add(scan)
        await db.flush()
        schedule.last_scan_id = scan.id
        schedule.last_outcome = 'queued'
    # Coalesce missed occurrences; never flood the queue after downtime.
    step = timedelta(minutes=schedule.interval_minutes)
    schedule.next_run_at += step * ((now - schedule.next_run_at) // step + 1)
    await db.commit()

async def dispatch_due(sessions, now=None):
    now = database_utc(now)
    async with sessions() as db:
        ids = list((await db.scalars(select(ScanSchedule.domain_id).where(
            ScanSchedule.enabled.is_(True), ScanSchedule.next_run_at <= now)
            .order_by(ScanSchedule.next_run_at).limit(100))).all())
    for domain_id in ids:
        async with sessions() as db:
            await dispatch_one(db, domain_id, now)

async def main():
    configure_logging()
    from app.database import SessionLocal, engine
    try:
        emit('scheduler', 'scheduler_started')
        await dispatch_due(SessionLocal)
        emit('scheduler', 'scheduler_completed')
    finally:
        await engine.dispose()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception:
        emit('scheduler', 'scheduler_failed')
        raise SystemExit(1) from None
