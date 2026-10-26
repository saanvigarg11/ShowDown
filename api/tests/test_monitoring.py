"""Privacy, dependency, probe and persisted-metric tests; no Azure or Slack calls."""
import asyncio
import io
import json
import logging
import os
import sys
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/'api'), str(ROOT/'shared'), str(ROOT/'worker')]
os.environ['DATABASE_URL'] = 'sqlite+aiosqlite:///:memory:'
os.environ['REDIS_URL'] = 'redis://127.0.0.1:6379/0'
with patch('sqlalchemy.ext.asyncio.create_async_engine', return_value=MagicMock()):
    from app.main import app
    from worker import main
from app import health as api_health
from worker.health import WorkerHealth
from worker import notifications
from shared import monitoring
from shared.models import Base, User, Domain, Scan, ScanStatus, ScanSchedule, ScanNotification
from shared.time_utils import database_utc

class LoggingTests(unittest.TestCase):
    def test_only_allowlisted_fields_and_states_can_reach_logs(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            monitoring.emit('worker', 'scan_failed', duration_ms=12, domain='private.example',
                token='secret', exception=RuntimeError('secret'), state={'secret':'value'}, status_code=float('nan'))
            monitoring.emit('worker', 'private.example', token='secret')
        record = json.loads(stream.getvalue())
        self.assertEqual(record['duration_ms'],12)
        self.assertNotIn('secret',stream.getvalue()); self.assertNotIn('private',stream.getvalue())
        self.assertNotIn('status_code',record)

    def test_library_formatter_drops_urls_arguments_and_tracebacks(self):
        record = logging.LogRecord('httpx',logging.ERROR,'file',1,'POST https://secret %s',('private.example',),None)
        record.exc_text='password=secret'
        result=monitoring.SafeFormatter().format(record)
        self.assertNotIn('secret',result); self.assertNotIn('private',result)
        self.assertEqual(json.loads(result)['level'],'ERROR')

    def test_logging_output_failure_does_not_raise(self):
        with patch('builtins.print', side_effect=OSError('closed')):
            monitoring.emit('worker','scan_completed')

class MonitoringTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine=create_async_engine('sqlite+aiosqlite:///:memory:')
        async with self.engine.begin() as conn: await conn.run_sync(Base.metadata.create_all)
        self.sessions=async_sessionmaker(self.engine,expire_on_commit=False)
        self.now=database_utc()
        async with self.sessions() as db:
            user=User(username='monitor-only',password_hash='unused')
            db.add(user); await db.flush()
            domain=Domain(name='private.example',owner_id=user.id,verified=True,verification_token='private-token')
            db.add(domain); await db.flush()
            self.owner=user.id; self.domain=domain.id
            await db.commit()
    async def asyncTearDown(self): await self.engine.dispose()

    async def test_dependency_success_failure_and_bounded_timeout(self):
        redis=MagicMock(); redis.ping=AsyncMock(return_value=True)
        self.assertEqual((await monitoring.dependencies(self.sessions,redis))['healthy'],1)
        redis.ping=AsyncMock(side_effect=RuntimeError('secret database URL'))
        state=await monitoring.dependencies(self.sessions,redis)
        self.assertEqual(state,{'database_ok':1,'redis_ok':0,'healthy':0})
        async def immediate_timeout(coro, timeout):
            coro.close()
            raise asyncio.TimeoutError()
        with patch.object(monitoring.asyncio,'wait_for',side_effect=immediate_timeout):
            state=await monitoring.dependencies(self.sessions,redis)
            self.assertEqual(state['database_ok'],0)

    async def test_persisted_counts_lateness_and_duration(self):
        async with self.sessions() as db:
            scans=[Scan(domain_id=self.domain,status=status,created_at=self.now-timedelta(minutes=20),
                completed_at=self.now if status in (ScanStatus.completed,ScanStatus.failed) else None)
                for status in ScanStatus]
            db.add_all(scans); await db.flush()
            db.add(ScanSchedule(domain_id=self.domain,owner_id=self.owner,enabled=True,interval_minutes=60,
                next_run_at=self.now-timedelta(minutes=16)))
            db.add(ScanNotification(scan_id=scans[0].id,domain_id=self.domain,owner_id=self.owner,
                events=[],status='failed',created_at=self.now))
            db.add(ScanNotification(scan_id=scans[1].id,domain_id=self.domain,owner_id=self.owner,
                events=[],status='sending',created_at=self.now,last_attempt_at=self.now-timedelta(minutes=6)))
            db.add(ScanNotification(scan_id=scans[2].id,domain_id=self.domain,owner_id=self.owner,
                events=[],status='pending',created_at=self.now,next_attempt_at=self.now-timedelta(minutes=16)))
            await db.commit()
        data=await monitoring.snapshot(self.sessions,self.now)
        for key in ('completed_count','failed_count','queued_count','running_count','overdue_schedules',
                    'notification_failures','notification_stuck','notification_overdue','old_queued_count'):
            self.assertEqual(data[key],1,key)
        self.assertAlmostEqual(data['scan_duration_seconds'],1200,places=2)
        self.assertEqual(data['oldest_running_seconds'],1200)
        self.assertNotIn('private',json.dumps(data))
        async with self.sessions() as db:
            (await db.get(ScanSchedule,self.domain)).enabled=False; await db.commit()
        self.assertEqual((await monitoring.snapshot(self.sessions,self.now))['overdue_schedules'],0)

    async def test_api_probes_and_failed_request_do_not_disclose_inputs(self):
        async def boom(request): raise RuntimeError('private.example password=secret')
        app.add_route('/monitor-test-failure',boom)
        route=app.routes[-1]
        stream=io.StringIO()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                with redirect_stdout(stream), patch.object(api_health,'dependencies',AsyncMock(return_value={'healthy':0})):
                    self.assertEqual((await client.get('/health/live')).status_code,200)
                    response=await client.get('/health/ready')
                    self.assertEqual(response.status_code,503)
                    self.assertEqual(response.json(),{'status':'unavailable'})
                    response=await client.get('/monitor-test-failure?token=secret')
                    self.assertEqual(response.status_code,500)
                    self.assertNotIn('secret',response.text)
                self.assertNotIn('secret',stream.getvalue()); self.assertNotIn('private',stream.getvalue())
                self.assertEqual(json.loads(stream.getvalue())['status_code'],500)
        finally:
            app.routes.remove(route)

    async def test_worker_http_probe_and_stalled_loop(self):
        state=WorkerHealth(10)
        server=await asyncio.start_server(state.probe,'127.0.0.1',0,limit=1024)
        port=server.sockets[0].getsockname()[1]
        try:
            async with httpx.AsyncClient() as client:
                self.assertEqual((await client.get(f'http://127.0.0.1:{port}/health/live')).status_code,200)
                self.assertEqual((await client.get(f'http://127.0.0.1:{port}/health/ready')).status_code,503)
                state.healthy=True
                self.assertEqual((await client.get(f'http://127.0.0.1:{port}/health/ready')).status_code,200)
                state.last_progress-=11
                self.assertEqual((await client.get(f'http://127.0.0.1:{port}/health/live')).status_code,503)
        finally:
            server.close(); await server.wait_closed()

    async def test_failed_collection_emits_unhealthy_without_exception_text(self):
        state=WorkerHealth(10)
        with patch('worker.health.dependencies',AsyncMock(return_value={'healthy':1,'database_ok':1,'redis_ok':1})), patch('worker.health.snapshot',AsyncMock(side_effect=RuntimeError('secret'))), patch('worker.health.asyncio.sleep',AsyncMock(side_effect=asyncio.CancelledError)), patch('worker.health.emit') as emit:
            with self.assertRaises(asyncio.CancelledError): await state.monitor(self.sessions,MagicMock())
        self.assertFalse(state.healthy)
        self.assertEqual(emit.call_args.kwargs['healthy'],0)
        self.assertNotIn('secret',str(emit.call_args_list))

    async def test_scan_completion_and_failure_emit_once_after_commit(self):
        for succeeds in (True, False):
            async with self.sessions() as db:
                scan=Scan(domain_id=self.domain,status=ScanStatus.queued)
                db.add(scan); await db.commit(); scan_id=scan.id
            output=dict(subdomains=[],open_ports={},socket_findings={},tls_findings={})
            pipeline=AsyncMock(return_value=output) if succeeds else AsyncMock(side_effect=TimeoutError('secret private.example'))
            with patch.object(main,'SessionLocal',self.sessions),patch.object(main,'SLACK_WEBHOOK_URL',''),patch.object(main,'ownership_matches',AsyncMock(return_value=True)),patch.object(main,'run_pipeline',pipeline),patch.object(main,'emit') as emit:
                await main.process_job({'scan_id':str(scan_id)})
                await main.process_job({'scan_id':str(scan_id)})
                events=[call.args[1] for call in emit.call_args_list]
                self.assertEqual(events,['scan_started','scan_completed' if succeeds else 'scan_failed'])
                self.assertGreaterEqual(emit.call_args.kwargs['duration_ms'],0)
                self.assertNotIn('secret',str(emit.call_args_list))

    async def test_notification_failure_emits_safe_state(self):
        async with self.sessions() as db:
            scan=Scan(domain_id=self.domain,status=ScanStatus.completed)
            db.add(scan); await db.flush()
            db.add(ScanNotification(scan_id=scan.id,domain_id=self.domain,owner_id=self.owner,events=[{'key':'one','kind':'risk','text':'private.example'}]))
            await db.commit(); scan_id=scan.id
        with patch.object(notifications,'emit') as emit:
            await notifications.deliver_one(self.sessions,scan_id,AsyncMock(return_value={'status':'failed','error':'private webhook'}))
            self.assertEqual(emit.call_args.kwargs['state'],'failed')
            self.assertNotIn('private',str(emit.call_args_list))
