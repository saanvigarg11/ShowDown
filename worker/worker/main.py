import asyncio
import os
import uuid
import time
from contextlib import suppress
from shared.monitoring import configure_logging, emit
from worker.health import WorkerHealth
from datetime import timedelta
from shared.targets import normalize_domain, in_scope, public_addresses, ownership_matches
from shared.time_utils import database_utc
from shared.auth_migration import require_auth_schema

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import select, or_, and_

from worker.queue_client import get_next_job, client as queue_redis
from worker.tasks.subdomain_enum import enumerate_subdomains
from worker.tasks.port_scan import scan_hosts as port_scan_hosts
from worker.tasks.socket_scan import scan_hosts as socket_scan_hosts
from worker.tasks.tls_check import check_tls
from worker.tasks.diff_engine import diff_scans
from worker.notifications import events_for, slack_delivery, deliver_pending

# Shared ORM models (installed from the /shared package in the Dockerfile)
# so the worker writes to the exact same schema the API reads from.
from shared.models import Scan, ScanStatus, Domain, User, ScanNotification  # noqa: F401

DATABASE_URL = os.environ["DATABASE_URL"]
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

engine = create_async_engine(DATABASE_URL)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def send_alert(message: str):
    return await slack_delivery(SLACK_WEBHOOK_URL, message)


SCAN_TIMEOUT = max(10, int(os.environ.get("SCAN_TIMEOUT_SECONDS", "900")))
MAX_ATTEMPTS = 3
health = WorkerHealth(SCAN_TIMEOUT + 120)

async def run_pipeline(domain_name: str) -> dict:
    addresses, warnings = {}, []
    candidates = await enumerate_subdomains(domain_name, warnings=warnings)
    for host in candidates[:1000]:
        try:
            host = normalize_domain(host)
            if not in_scope(host, domain_name):
                raise ValueError("Out-of-scope hostname rejected")
            ips = await public_addresses(host)
            if ips:
                addresses[host] = ips
            else:
                warnings.append({"host": host, "stage": "dns", "error": "No A or AAAA records"})
        except Exception as exc:
            warnings.append({"host": host, "stage": "dns", "error": type(exc).__name__})
    if len(candidates) > 1000:
        warnings.append({"stage": "discovery", "error": "1000-host limit reached"})
    if not addresses:
        raise RuntimeError("No safe publicly resolved hosts; scan not performed")
    open_ports = await port_scan_hosts(list(addresses), addresses=addresses)
    socket_findings = await socket_scan_hosts(open_ports, addresses=addresses)
    tls_findings = {}
    for host, ports in open_ports.items():
        tls_ports = [p for p in ports if p in {443, 8443, 465, 993, 995}]
        tls_findings[host] = []
        for address in addresses[host]:
            tls_findings[host].extend(await check_tls(host, tls_ports, address=address))
    return dict(subdomains=sorted(addresses), open_ports=open_ports,
                socket_findings=socket_findings, tls_findings=tls_findings,
                resolved_addresses=addresses, warnings=warnings)

async def process_job(job: dict):
    health.progress()
    started = time.monotonic()
    try:
        scan_id = uuid.UUID(str(job['scan_id']))
    except (KeyError, ValueError, TypeError):
        emit('worker', 'invalid_job')
        return
    async with SessionLocal() as db:
        # Row lock serializes duplicate deliveries; persisted lease permits crash recovery.
        result = await db.execute(select(Scan).where(Scan.id == scan_id).with_for_update())
        scan = result.scalars().first()
        if not scan or scan.status in (ScanStatus.completed, ScanStatus.failed):
            return
        metadata = scan.diff or {}
        now = database_utc()
        lease = metadata.get('lease_started')
        if scan.status == ScanStatus.running and lease:
            from datetime import datetime
            try:
                if now - datetime.fromisoformat(lease) < timedelta(seconds=SCAN_TIMEOUT + 60):
                    return
            except ValueError:
                pass
        attempts = metadata.get('attempts', 0) + 1
        if attempts > MAX_ATTEMPTS:
            scan.status = ScanStatus.failed
            scan.completed_at = now
            scan.diff = dict(metadata, error='Interrupted job retry limit exceeded')
            await db.commit()
            emit('worker', 'scan_failed', attempt=attempts, duration_ms=(time.monotonic()-started)*1000)
            return
        domain = await db.get(Domain, scan.domain_id)
        owner = await db.get(User, domain.owner_id) if domain and domain.owner_id else None
        if not domain or not domain.verified or not owner or not owner.active:
            scan.status = ScanStatus.failed
            scan.completed_at = now
            scan.diff = {'error': 'Domain must have an active owner and DNS verification', 'attempts': attempts}
            await db.commit()
            emit('worker', 'scan_failed', attempt=attempts, duration_ms=(time.monotonic()-started)*1000)
            return
        # Never trust domain names supplied by queue clients.
        domain_name = domain.name
        token = domain.verification_token
        scan.status = ScanStatus.running
        scan.diff = {'attempts': attempts, 'lease_started': now.isoformat()}
        await db.commit()
        emit('worker', 'scan_started', attempt=attempts)
        try:
            async with asyncio.timeout(SCAN_TIMEOUT):
                domain_name = normalize_domain(domain_name)
                if not await ownership_matches(domain_name, token):
                    raise PermissionError('Ownership TXT record no longer matches')
                results = await run_pipeline(domain_name)
            prev_result = await db.execute(select(Scan).where(
                Scan.domain_id == scan.domain_id, Scan.status == ScanStatus.completed,
                or_(Scan.created_at < scan.created_at, and_(Scan.created_at == scan.created_at, Scan.id < scan_id))
                ).order_by(Scan.created_at.desc(), Scan.id.desc()).limit(1))
            previous = prev_result.scalars().first()
            previous_snapshot = None if not previous else dict(
                scan_id=str(previous.id), domain=domain_name, subdomains=previous.subdomains,
                open_ports=previous.open_ports, socket_findings=previous.socket_findings,
                tls_findings=previous.tls_findings, diff=previous.diff,
                observed_at=(previous.completed_at or previous.created_at).isoformat())
            results['domain'] = domain_name
            results['observed_at'] = database_utc().isoformat()
            diff = diff_scans(previous_snapshot, results)
            # Additive metadata avoids a migration and keeps existing result contracts.
            diff.update(attempts=attempts, resolved_addresses=results.get('resolved_addresses', {}),
                        warnings=results.get('warnings', []))
            current = await db.execute(select(Scan).where(Scan.id == scan_id).with_for_update().execution_options(populate_existing=True))
            scan = current.scalars().first()
            if not scan or scan.status != ScanStatus.running or (scan.diff or {}).get('attempts') != attempts:
                return
            scan.subdomains = results['subdomains']
            scan.open_ports = results['open_ports']
            scan.socket_findings = results['socket_findings']
            scan.tls_findings = results['tls_findings']
            scan.diff = diff
            scan.status = ScanStatus.completed
            scan.completed_at = database_utc()
            if SLACK_WEBHOOK_URL:
                notification_events = events_for(domain_name, diff, previous_snapshot)
                if notification_events:
                    db.add(ScanNotification(scan_id=scan.id, domain_id=domain.id,
                        owner_id=domain.owner_id, events=notification_events))
            await db.commit()
            emit('worker', 'scan_completed', attempt=attempts, duration_ms=(time.monotonic()-started)*1000)
            health.progress()
        except Exception as exc:
            await db.rollback()
            current = await db.execute(select(Scan).where(Scan.id == scan_id).with_for_update().execution_options(populate_existing=True))
            scan = current.scalars().first()
            if scan is None or scan.status != ScanStatus.running or (scan.diff or {}).get('attempts') != attempts:
                return
            scan.status = ScanStatus.failed
            scan.completed_at = database_utc()
            scan.diff = {'error': str(exc) or type(exc).__name__, 'error_type': type(exc).__name__, 'attempts': attempts}
            await db.commit()
            emit('worker', 'scan_failed', attempt=attempts, duration_ms=(time.monotonic()-started)*1000)
            health.progress()
            return
        # Notification failure must never invalidate a committed scan.
        try:
            if SLACK_WEBHOOK_URL:
                await deliver_pending(SessionLocal, send_alert)
        except Exception:
            emit('notifications', 'notification_failure')

async def recover_pending():
    # PostgreSQL is the durable source of queued/running work. A Redis outage or
    # crash after BLPOP cannot strand a scan. Active leases are skipped by claim.
    async with SessionLocal() as db:
        result = await db.execute(select(Scan.id).where(Scan.status.in_([
            ScanStatus.queued, ScanStatus.running])).order_by(Scan.created_at).limit(100))
        ids = list(result.scalars().all())
    for scan_id in ids:
        try:
            await process_job({'scan_id': str(scan_id)})
        except Exception:
            emit('worker', 'worker_failure')

async def main():
    configure_logging()
    async with engine.connect() as connection:
        await connection.run_sync(require_auth_schema)
    server = await asyncio.start_server(health.probe, '0.0.0.0', 8001, limit=1024)
    monitor = asyncio.create_task(health.monitor(SessionLocal, queue_redis))
    try:
        await polling_loop()
    finally:
        monitor.cancel()
        with suppress(asyncio.CancelledError):
            await monitor
        server.close()
        await server.wait_closed()

async def polling_loop():
    last_recovery = 0
    while True:
        health.progress()
        try:
            if asyncio.get_running_loop().time() - last_recovery > 30:
                await recover_pending()
                if SLACK_WEBHOOK_URL:
                    await deliver_pending(SessionLocal, send_alert)
                last_recovery = asyncio.get_running_loop().time()
            job = await get_next_job(timeout=5)
            if job:
                await process_job(job)
        except Exception:
            emit('worker', 'worker_failure')
            await asyncio.sleep(5)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception:
        emit('worker', 'startup_failed')
        raise SystemExit(1) from None
