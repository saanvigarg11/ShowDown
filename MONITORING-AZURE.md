# Centralized monitoring — ShowDown / frontend-updates

## Implementation

API, worker, scheduler and notification delivery emit allowlisted JSON to stdout
under `schema: showdown.monitor.v1`. The existing Container Apps environment sends
console logs to the existing Log Analytics workspace; there is no new workspace,
Application Insights SDK, external telemetry vendor, database migration or Slack
dependency. Existing scan, scheduling, authentication and delivery semantics remain.

The new stream excludes targets, hostnames, IP addresses, scan/domain/user IDs,
request paths/query strings, bodies, headers, tokens, SQL parameters, exception
messages/tracebacks and webhook URLs. Only fixed event/component/state values and
numeric measurements are accepted. Library logs are reduced to severity markers;
Uvicorn access logging is disabled at runtime. This deliberately trades detailed
console debugging for privacy: inspect owner-authorized scan history for scan
evidence, not centralized operational logs. Existing separate honeypot audit records
and gateway/platform logs are not redefined by this monitoring stream. Their access
and retention policies must still be reviewed separately; do not export raw audit
payloads into report screenshots. No current secrets were read or logged for setup.

## Health and metrics

| Signal | Behavior |
|---|---|
| API `/health/live` | Returns 200 if the API process can respond; independent of DB/Redis outages. |
| API `/health/ready` | Concurrent SELECT 1 and Redis PING, each bounded to 3 seconds; 200 or generic 503, no dependency addresses/details. Through gateway: `/api/health/ready`. |
| Worker port 8001 | Internal HTTP `/health/live` and `/health/ready`; no public ingress, authentication/data endpoints or Docker host port mapping added. |
| Worker liveness | Event loop responds and processing progress is no older than SCAN_TIMEOUT_SECONDS + 120 seconds. Normal long scans within timeout remain alive. |
| Worker readiness | Dependency checks, metric collection and loop progress are healthy; initial state is not ready. |
| Heartbeats | API and worker emit every approximately 60 seconds. Worker readiness is cached from this cycle. |
| Scan events | Started/completed/failed; terminal events emitted after commit, with attempt number and processing elapsed milliseconds (not target identity). |
| Scan gauges | Queued/running totals, completed/failed in the previous 15 minutes, oldest running age and maximum completed scan lifetime including queue wait. |
| Missed schedules | Count of enabled schedules whose next_run_at is overdue by more than 15 minutes, even if the scheduler job never starts. |
| Notification state | Safe delivery state/attempt events; failed/uncertain/expired records created within 24h, sending claims stuck >5m, pending and overdue counts. |
| Scheduler events | Start/completion/failure and DNS deferral; no authorization tokens or domains logged. |

`MONITOR_SCHEDULE_GRACE_SECONDS` defaults to 900 (bounded 1–86400) and also controls
old-queued and overdue-notification thresholds in the worker. Invalid integers use
the default. No deployment variable wiring was added for this optional override;
an authorized operator can supply it to the worker when needed.

Metrics read existing scan/schedule/notification tables, with no writes. Those
previously implemented schemas must exist. Missing tables/timeouts cause an
unhealthy monitoring sample rather than invented zeros. Database dependency failure
does not trigger liveness restarts by itself. Worker aggregate failures are logged
without exception text. Aggregate queries are suitable for current modest history;
measure query costs and consider indexes/retention before large-scale use.

Counts are rolling gauges: use maximum/latest, never sum repeated heartbeat counts.
Processing-event durations and persisted lifetime durations measure different things.
Duplicate delivery of a completed scan emits no second completion event. Event logs
can still be lost on a process crash immediately after commit; persisted gauges
provide a restart-tolerant cross-check. No exactly-once telemetry claim is made.

## Terraform / alert activation

`infra/monitoring.tf` uses `azurerm_monitor_scheduled_query_rules_alert_v2` scoped to
the existing workspace. `monitoring_enabled` defaults to **false**: no alert resources
or custom probes are enabled until an operator opts in. New code still emits JSON.

Example non-secret inputs (do not apply without separate authorization):

```hcl
monitoring_enabled = true
monitoring_action_group_ids = [] # portal-only; or approved existing Azure action-group IDs
monitoring_thresholds = {
  api_errors    = 5
  scan_failures = 3
  scan_duration = 1200
}
```

Defaults are 11 rules, plus 2 scheduler rules only when scheduled_scanning_enabled
is true. Evaluation runs every five minutes. Threshold overrides use the keys below:

| Rule | Default condition |
|---|---|
| api_errors | >=5 HTTP 5xx events in 15m |
| api_unhealthy / worker_unhealthy | >=3 unhealthy samples in 15m |
| worker_errors | >=3 worker errors/startup failures in 15m |
| scan_failures | >=3 persisted failures in a rolling 15m sample |
| scan_duration | >=1200 seconds completed lifetime or oldest running age |
| queue_delayed | >=1 queued scan older than grace period |
| missed_schedules | >=1 enabled overdue schedule |
| notifications | >=1 recent failure, uncertain/expired record, stuck claim or overdue delivery |
| api_missing / worker_missing | Fewer than 1 heartbeat in 15m |
| scheduler_missing | Fewer than 1 completed dispatch tick in 30m |
| scheduler_errors | >=1 failed/deferred tick event in 15m |

Rules are stateful and auto-resolve; repetitive unhealthy samples do not create a
new notification per sample. Counts sampled in a log lookback can extend the time
before recovery resolves. No-data rules distinguish silence from healthy idleness,
but ingestion outages/caps can also trigger them. No per-target alert dimensions.

Action-group IDs are optional. With an empty list, alerts are visible in Azure
Monitor but no email/Slack/SMS is sent. Reuse an approved email or other action group
for independent outage notification. Existing scan Slack notifications remain
optional and separate; never paste a Slack webhook into an Azure common-alert
webhook receiver without an approved payload adapter. No action group was created
and no real notifications were sent here.

Queries use ContainerAppConsoleLogs_CL / Log_s, matching the existing direct
Log Analytics configuration. They filter the existing core/job names. Confirm the
actual table and scheduler name column in staging (ContainerAppName_s or
ContainerJobName_s). Newer Azure Monitor log destinations may use unsuffixed tables;
adapt queries only after inspecting that environment. Terraform retains server-side
query validation: a missing table/schema must be resolved, not bypassed.

Activation order: package updated shared/API/worker images with immutable tags;
verify prerequisite tables and healthy application in an authorized environment;
confirm logs arrive; then review a complete Terraform plan and authorize the probes
and rules separately. No plan/apply/destroy or cloud login was run for this work.
Both API and worker gain custom liveness/readiness probes when monitoring is enabled.
Review failure behavior carefully: all core containers share one Container App, so
an unhealthy worker can affect readiness of its core replica. No resource in the
untracked infra-dev/ directory was edited or used as a deployment target.

## Permissions and costs

- Runtime: existing database reads/Redis connectivity/stdout logging only. No cloud
  write permissions, telemetry keys or new managed identities are needed by code.
- Operator: scoped Microsoft.App container configuration rights for probe/image
  rollout; Microsoft.Insights scheduled query rule write/read rights (for example
  Monitoring Contributor), workspace query/read permissions and access to chosen
  action groups. Microsoft.Insights must be registered by an authorized subscription
  operator if absent. No role assignments or provider registrations were performed.
- Report viewer: Log Analytics Reader (or narrower permitted query access) plus
  Monitoring Reader for alerts. Access is aggregate across the installation; do not
  grant ordinary dashboard users unrestricted workspace access.
- Costs: reuse existing compute/workspace. Logs add ingestion and retention usage;
  11–13 log alert rules incur evaluation charges; action-group channels may incur
  notification charges. Defaults retain the existing 30-day retention and 0.5 GB/day
  workspace cap. This cap is not a total budget guarantee and can stop ingestion,
  making monitoring incomplete. There are about 2,880 heartbeat records/day for one
  API and worker, plus requests/events. Estimate actual byte volume and region rates
  before enabling; no exact bill is claimed and no paid resources were activated.

References checked:
- https://learn.microsoft.com/azure/container-apps/log-monitoring
- https://learn.microsoft.com/azure/container-apps/health-probes
- https://learn.microsoft.com/azure/azure-monitor/alerts/alerts-overview
- https://learn.microsoft.com/azure/azure-monitor/logs/manage-access
- https://azure.microsoft.com/pricing/details/monitor/

## Verification for the project report

Local commands, from api/ using the existing backend test environment:

```powershell
python -m unittest discover -s tests -p test_monitoring.py -v
python -m unittest discover -s tests -q
```

Tests use isolated SQLite, mocked DNS/scans/Slack/dependencies and a loopback-only
worker probe server. No production data, scanners, migrations or alerts are used.
Terraform validation is performed on an isolated copy with backend disabled, not
on the user's live state. This verifies provider/HCL compatibility, NOT Azure KQL
execution, permissions or delivery. PostgreSQL aggregate SQL and concurrency still
need staging validation; SQLite is not evidence of Azure database execution.

For a later, separately authorized staging demonstration:
1. Capture both healthy endpoints and redacted heartbeat JSON. Show the unchanged
   normal scan flow. Record image tags, UTC timestamps and test totals.
2. Open Log Analytics and run infra/monitoring-report.kql. Capture successes,
   failures, durations and gauges. Inspect a sample for absence of secrets/targets.
3. Against a dedicated test database/fixture environment, simulate a failed scan,
   overdue enabled schedule and failed notification; do not create these in
   production or scan arbitrary domains. Show matching aggregate query results.
4. In isolated staging only, interrupt the worker/dependency to show 503 readiness,
   missing/unhealthy alerts, then restoration and automatic resolution. Distinguish
   no-data caused by stopped code from ingestion loss using platform system logs.
5. Keep action groups empty for portal-only alert evidence. Real channel delivery
   requires separate recipient approval; no test Slack alert is necessary.
6. Label report evidence accurately: local regression tests and Terraform validation
   are verified here; live Azure ingestion, KQL, probe rollout, alert firing and
   channel delivery are pending until performed by your Azure-authorized teammate.

Files: shared/shared/monitoring.py; api/app/health.py; api/app/main.py;
worker/worker/health.py; worker/worker/main.py; api/app/scheduler.py;
worker/worker/notifications.py; infra/main.tf; infra/monitoring.tf;
infra/monitoring-report.kql; api/tests/test_monitoring.py; this document.

Verified results: all 102 backend tests passed; the 10 monitoring tests also passed
independently after the final assertion updates. Terraform validate passed in the
isolated copy using AzureRM 4.81.0/random 3.9.1, and formatting of monitoring.tf and
git diff --check passed. Existing .gitignore changes, infra-dev/, showdown_backup.sql,
frontend files and the repository's Terraform lock/state files were left untouched.
No migrations, production modifications, real alerts, deployments, commits or
pushes were performed. Frontend tests/build were not rerun: frontend was unchanged.
