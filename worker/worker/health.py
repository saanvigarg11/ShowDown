"""Internal-only HTTP probes plus aggregate heartbeat; no target data is exposed."""
import asyncio
import time
from shared.monitoring import dependencies, snapshot, emit

class WorkerHealth:
    def __init__(self, max_idle):
        self.last_progress = time.monotonic()
        self.max_idle = max_idle
        self.healthy = False

    def progress(self):
        self.last_progress = time.monotonic()

    def loop_ok(self):
        return int(time.monotonic()-self.last_progress <= self.max_idle)

    async def probe(self, reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=2)
            parts = line.split()
            path = parts[1] if len(parts) == 3 and parts[0] == b'GET' else b''
            if path == b'/health/live':
                code = 200 if self.loop_ok() else 503
            elif path == b'/health/ready':
                code = 200 if self.healthy and self.loop_ok() else 503
            else:
                code = 404
            body = b'{"status":"ok"}' if code == 200 else b'{"status":"unavailable"}'
            writer.write(f'HTTP/1.1 {code} Response\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode()+body)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def monitor(self, sessions, redis):
        while True:
            state = await dependencies(sessions, redis)
            state['loop_ok'] = self.loop_ok()
            self.healthy = bool(state['healthy'] and state['loop_ok'])
            state['healthy'] = int(self.healthy)
            try:
                counts = await asyncio.wait_for(snapshot(sessions), timeout=10)
            except Exception:
                counts = {}
                self.healthy = False
                state['healthy'] = 0
                emit('worker', 'worker_failure')
            emit('worker', 'worker_health', **state, **counts)
            await asyncio.sleep(60)
