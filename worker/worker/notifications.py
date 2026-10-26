"""Scan-derived Slack notifications. No webhook or raw response is persisted/logged."""
import hashlib
import math
import os
from datetime import timedelta
import httpx
from sqlalchemy import select
from shared.models import Domain, User, ScanNotification
from shared.time_utils import database_utc
from shared.monitoring import emit
from worker.tasks.diff_engine import build_alert_message
from worker.tasks.risk_engine import canonical, score_snapshot

def setting(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, default))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))

def events_for(domain, diff, previous):
    events = []
    def add(kind, identity, text):
        key = hashlib.sha256(canonical([kind, identity]).encode()).hexdigest()
        events.append(dict(key=key, kind=kind, text=text))
    legacy = build_alert_message(domain, diff)
    if legacy:
        add('changes', [diff.get('new_subdomains'), diff.get('new_open_ports')], legacy)
    risk = diff.get('risk') or {}
    if previous is not None:
        old = (previous.get('diff') or {}).get('risk') or score_snapshot(previous)
        before, after = old.get('score', 0), risk.get('score', 0)
        delta = setting('ALERT_RISK_INCREASE', 20, 1, 100)
        escalations = [(f['id'], f['score']) for f in risk.get('findings', [])
            if f['score'] >= 70 and f['score'] > next((p['score'] for p in old.get('findings', []) if p['id'] == f['id']), 0)]
        crossed = any(before < threshold <= after for threshold in (70, 90))
        if (after >= 40 and after - before >= delta) or crossed or escalations:
            add('risk', [after, sorted(escalations)],
                f'Risk escalation for {domain}: {before} -> {after}/100 ({risk.get("severity")}). '
                f'{len(escalations)} new or escalated High/Critical findings. Review scan evidence.')
    for finding in risk.get('findings', []):
        if finding.get('rule_id') != 'certificate_expiry':
            continue
        evidence = finding.get('evidence') or {}
        days = evidence.get('days_until_expiry')
        if isinstance(days, bool) or not isinstance(days, (int, float)) or not math.isfinite(days) or days > 30:
            continue
        bucket = 'expired' if days < 0 else 'within 7 days' if days <= 7 else 'within 30 days'
        add('certificate', [finding['id'], evidence.get('expires'), bucket],
            f'TLS certificate {bucket}: {finding["host"]}:{finding["port"]} '
            f'({finding.get("address") or "address unavailable"}); expires {evidence.get("expires") or "date unavailable"}; '
            f'{days} days remaining ({finding["severity"]}).')
    return events

async def slack_delivery(webhook, message):
    if not webhook:
        return {'status': 'disabled'}
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.post(webhook, json={'text': message})
    except httpx.TransportError:
        # Slack has no idempotency key. The request might have been accepted.
        return {'status': 'uncertain', 'error': 'transport outcome unknown'}
    if response.status_code == 200 and response.text.strip() == 'ok':
        return {'status': 'sent'}
    if response.status_code == 429:
        try:
            delay = max(60, int(response.headers.get('Retry-After', '60')))
        except ValueError:
            delay = 60
        return {'status': 'retry', 'delay': min(delay, 604800), 'error': 'Slack rate limited'}
    if response.status_code >= 500:
        return {'status': 'retry', 'error': 'Slack server failure'}
    return {'status': 'failed', 'error': f'Slack rejected delivery ({response.status_code})'}

async def deliver_one(sessions, scan_id, sender, now=None):
    now = database_utc(now)
    async with sessions() as db:
        item = await db.get(ScanNotification, scan_id)
        if item is None:
            return
        # Serialize per-domain claims across workers, then reload stale ORM state.
        domain = (await db.execute(select(Domain).where(Domain.id == item.domain_id).with_for_update())).scalar_one_or_none()
        await db.refresh(item)
        if item.status != 'pending' or item.next_attempt_at > now:
            return
        if item.created_at < now-timedelta(days=1):
            item.status = 'expired'
            item.last_error = 'Notification older than 24 hours; consult scan history'
            await db.commit()
            return
        owner = await db.get(User, item.owner_id)
        if not domain or domain.owner_id != item.owner_id or not owner or not owner.active:
            item.status = 'cancelled'
            await db.commit()
            return
        history = list((await db.scalars(select(ScanNotification).where(
            ScanNotification.domain_id == item.domain_id,
            ScanNotification.owner_id == item.owner_id,
            ScanNotification.scan_id != item.scan_id,
            ScanNotification.created_at >= now-timedelta(days=30),
            ScanNotification.status.in_(['sent', 'sending', 'uncertain', 'pending'])))).all())
        accepted = [old for old in history if old.status != 'pending']
        seen = {event['key'] for old in accepted for event in old.events
            if event['kind'] == 'certificate' or (old.sent_at or old.last_attempt_at or old.created_at) >= now-timedelta(days=1)}
        item.events = [event for event in item.events if event['key'] not in seen]
        if not item.events:
            item.status = 'suppressed'
            await db.commit()
            return
        cooldown = setting('ALERT_COOLDOWN_SECONDS', 3600, 60, 86400)
        recent = max((old.sent_at or old.last_attempt_at or old.created_at for old in accepted), default=now-timedelta(days=31))
        retry_wait = max((old.next_attempt_at for old in history if old.status == 'pending' and old.attempts), default=now)
        permitted_at = max(recent + timedelta(seconds=cooldown), retry_wait)
        if permitted_at > now:
            item.next_attempt_at = permitted_at
            await db.commit()
            return
        item.attempts += 1
        item.last_attempt_at = now
        item.status = 'sending'
        # Claim commits before HTTP. Crash/unknown outcome is never auto-resubmitted.
        await db.commit()
        lines = [event['text'] for event in item.events[:20]]
        if len(item.events) > 20:
            lines.append(f'{len(item.events)-20} additional events; consult scan details.')
        message = f'ShowDown scan {scan_id}\n' + '\n'.join(lines)
        message = message[:12000].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        try:
            result = await sender(message)
        except Exception:
            result = {'status': 'uncertain', 'error': 'delivery outcome unknown'}
        item.last_error = result.get('error')
        if result['status'] == 'retry' and item.attempts < 3:
            item.status = 'pending'
            item.next_attempt_at = now + timedelta(seconds=max(60 * 2**(item.attempts-1), result.get('delay', 0)))
        else:
            item.status = 'failed' if result['status'] == 'retry' else result['status']
        if item.status == 'sent':
            item.sent_at = now
        await db.commit()
        emit('notifications', 'notification_result', state=item.status, attempt=item.attempts)

async def deliver_pending(sessions, sender, now=None):
    now = database_utc(now)
    async with sessions() as db:
        ids = list((await db.scalars(select(ScanNotification.scan_id).where(
            ScanNotification.status == 'pending', ScanNotification.next_attempt_at <= now)
            .order_by(ScanNotification.created_at).limit(100))).all())
    for scan_id in ids:
        await deliver_one(sessions, scan_id, sender, now)
