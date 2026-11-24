from datetime import UTC, datetime, timedelta
from time import perf_counter

from app.archive.manifest import batch_key, manifest
from app.monitor.inspection import Inspection
from app.monitor.source import input_packet


def _as_due_utc(value):
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace('Z', '+00:00')
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec='seconds')


def retention_due(payload):
    """Read optional retention instructions without changing batch identity."""
    if not isinstance(payload, dict):
        return None
    options = payload.get('retention') if isinstance(payload.get('retention'), dict) else {}
    if payload.get('expired') is True or payload.get('due') is True or options.get('expired') is True:
        return datetime.now(UTC).isoformat(timespec='seconds')
    day_keys = ('retention_days', 'retain_days', 'retention_period_days')
    days = next((payload[key] for key in day_keys if key in payload),
                options.get('days', options.get('retention_days')))
    due_keys = ('due_utc', 'retention_due_utc', 'retention_until', 'retain_until',
                'expires_utc', 'expires_at', 'due')
    due = next((payload[key] for key in due_keys if key in payload),
               options.get('due_utc', options.get('until')))
    if days is not None:
        try:
            return (datetime.now(UTC) + timedelta(days=float(days))).isoformat(timespec='seconds')
        except (TypeError, ValueError):
            pass
    return _as_due_utc(due)


def archive_run(store, payload):
    started = perf_counter()
    files, name = input_packet(payload)
    run_id, hashes = batch_key(files)
    record = store.lookup(run_id)
    if record is not None:
        return record, True
    audit = Inspection(files)
    results = audit.results()
    packet = {'manifest': manifest(run_id, hashes, vars(audit.window), name, started),
              'originals': files, 'results': results}
    return store.commit(run_id, packet, due_utc=retention_due(payload))
