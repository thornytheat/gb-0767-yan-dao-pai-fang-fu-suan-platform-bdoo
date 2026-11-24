import json
import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from app.archive.manifest import canonical
from app.errors import InputError


def utc_now():
    return datetime.now(UTC).isoformat(timespec='seconds')


class Archive:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS monitoring (id TEXT PRIMARY KEY, packet TEXT NOT NULL)')
            db.execute('''
                CREATE TABLE IF NOT EXISTS originals (
                    sha256 TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    bytes INTEGER NOT NULL DEFAULT 0,
                    created_utc TEXT NOT NULL
                )
            ''')
            db.execute('''
                CREATE TABLE IF NOT EXISTS run_originals (
                    run_id TEXT NOT NULL,
                    file_key TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_utc TEXT NOT NULL,
                    released_utc TEXT,
                    PRIMARY KEY (run_id, file_key)
                )
            ''')
            db.execute('CREATE INDEX IF NOT EXISTS idx_run_originals_hash ON run_originals(sha256, active)')
            db.execute('''
                CREATE TABLE IF NOT EXISTS retention (
                    run_id TEXT PRIMARY KEY,
                    due_utc TEXT,
                    issued_utc TEXT,
                    released_utc TEXT,
                    closed_utc TEXT,
                    created_utc TEXT NOT NULL,
                    updated_utc TEXT NOT NULL
                )
            ''')
            self._backfill(db)

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(self.path, timeout=15)) as db, db:
            yield db

    @contextmanager
    def immediate(self):
        # SQLite's ordinary write transaction can raise BUSY after another
        # worker wins the lock.  BEGIN IMMEDIATE makes read/modify/delete a
        # single serialization point for issuance+cleanup.
        with closing(sqlite3.connect(self.path, timeout=15)) as db:
            db.execute('PRAGMA busy_timeout = 15000')
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    def _backfill(self, db):
        now = utc_now()
        rows = db.execute('SELECT id, packet FROM monitoring').fetchall()
        for run_id, raw in rows:
            packet = json.loads(raw)
            db.execute('INSERT OR IGNORE INTO retention(run_id, created_utc, updated_utc) VALUES (?, ?, ?)',
                        (run_id, packet.get('manifest', {}).get('created_utc', now), now))
            self._ensure_original_rows(db, run_id, packet, now)

    def _ensure_original_rows(self, db, run_id, packet, now=None, activate=False):
        now = now or utc_now()
        files = packet.get('originals')
        hashes = packet.get('manifest', {}).get('sha256', {})
        if isinstance(files, dict):
            for file_key, content in files.items():
                if not isinstance(content, str):
                    continue
                digest = sha256(content.encode('utf-8')).hexdigest()
                db.execute('INSERT OR IGNORE INTO originals(sha256, content, bytes, created_utc) '
                           'VALUES (?, ?, ?, ?)', (digest, content, len(content.encode('utf-8')), now))
                db.execute('''
                    INSERT INTO run_originals(run_id, file_key, sha256, active, created_utc)
                    VALUES (?, ?, ?, 1, ?)
                    ON CONFLICT(run_id, file_key) DO UPDATE SET
                        sha256 = excluded.sha256,
                        active = CASE WHEN ? THEN 1 ELSE run_originals.active END,
                        released_utc = CASE WHEN ? THEN NULL ELSE run_originals.released_utc END
                ''', (run_id, file_key, digest, now, 1 if activate else 0, 1 if activate else 0))
        else:
            released = packet.get('cleaned_utc') or packet.get('manifest', {}).get('cleaned_utc')
            for file_key, digest in hashes.items():
                db.execute('INSERT OR IGNORE INTO run_originals'
                           '(run_id, file_key, sha256, active, created_utc, released_utc) '
                           'VALUES (?, ?, ?, 0, ?, ?)',
                           (run_id, file_key, digest, now, released))

    def lookup(self, run_id):
        with self.connection() as db:
            row = db.execute('SELECT packet FROM monitoring WHERE id = ?', (run_id,)).fetchone()
        return None if row is None else json.loads(row[0])

    def require(self, run_id):
        packet = self.lookup(run_id)
        if packet is None:
            raise InputError('RUN_UNKNOWN', '找不到监测留档', 404)
        return packet

    def commit(self, run_id, packet, due_utc=None):
        now = utc_now()
        with self.connection() as db:
            cursor = db.execute('INSERT INTO monitoring VALUES (?, ?) ON CONFLICT(id) DO NOTHING',
                                (run_id, canonical(packet)))
            reused = cursor.rowcount == 0
            db.execute('INSERT OR IGNORE INTO retention(run_id, due_utc, created_utc, updated_utc) '
                       'VALUES (?, ?, ?, ?)',
                       (run_id, due_utc or packet.get('manifest', {}).get('retention_due_utc'), now, now))
            if not reused:
                self._ensure_original_rows(db, run_id, packet, now, activate=True)
            record = db.execute('SELECT packet FROM monitoring WHERE id = ?', (run_id,)).fetchone()[0]
        return json.loads(record), reused

    def _retention_row(self, db, run_id):
        return db.execute('SELECT due_utc, issued_utc, released_utc, closed_utc '
                          'FROM retention WHERE run_id = ?', (run_id,)).fetchone()

    def update_retention(self, run_id, *, due_utc=None, set_due=False, issued=None,
                         closed=None, released=None):
        packet = self.require(run_id)
        now = utc_now()
        with self.connection() as db:
            row = self._retention_row(db, run_id)
            if row is None:
                db.execute('INSERT INTO retention(run_id, created_utc, updated_utc) VALUES (?, ?, ?)',
                           (run_id, now, now))
            fields, values = [], []
            if set_due:
                fields.append('due_utc = ?')
                values.append(due_utc)
            if issued is not None:
                fields.append('issued_utc = ?')
                values.append(now if issued is True else issued)
            if closed is not None:
                fields.append('closed_utc = ?')
                values.append(now if closed is True else (None if closed is False else closed))
            if released is not None:
                fields.append('released_utc = ?')
                values.append(now if released is True else (None if released is False else released))
            fields.append('updated_utc = ?')
            values.extend([now, run_id])
            db.execute(f'UPDATE retention SET {", ".join(fields)} WHERE run_id = ?', values)
            row = self._retention_row(db, run_id)
        return self._retention_dict(run_id, packet, row)

    @staticmethod
    def _retention_dict(run_id, packet, row):
        due, issued, released, closed = row or (None, None, None, None)
        cleaned = bool(released) or not packet.get('originals')
        due_at = Archive._parse_time(due)
        if cleaned:
            state, status = 'released', 'CLEANED'
        elif due_at is not None and due_at <= datetime.now(UTC):
            state, status = 'due', 'DUE'
        else:
            state, status = 'retained', 'ACTIVE'
        return {'run_id': run_id, 'due_utc': due, 'issued_utc': issued,
                'issued': issued is not None,
                'released_utc': released, 'closed_utc': closed,
                'cleaned_utc': released, 'cleaned_at': released, 'released_at': released,
                'cleaned': cleaned, 'is_cleaned': cleaned, 'retained': not cleaned,
                'closed': closed is not None, 'state': state, 'status': status,
                'case_open': closed is None}

    def release_originals(self, run_id, *, issue_certificate=False, close_case=False, now=None):
        """Release this batch's references and physically delete only unreferenced originals."""
        stamp = now or utc_now()
        with self.immediate() as db:
            row = db.execute('SELECT packet FROM monitoring WHERE id = ?', (run_id,)).fetchone()
            if row is None:
                raise InputError('RUN_UNKNOWN', '找不到监测留档', 404)
            packet = json.loads(row[0])
            retention_row = self._retention_row(db, run_id)
            already_cleaned = bool(retention_row and retention_row[2]) or not packet.get('originals')

            certificate = None
            if issue_certificate and not already_cleaned:
                from app.archive.certificate import issue
                certificate = issue(packet)

            hashes = set(packet.get('manifest', {}).get('sha256', {}).values())
            if not hashes and isinstance(packet.get('originals'), dict):
                hashes = {sha256(value.encode()).hexdigest() for value in packet['originals'].values()}

            if not already_cleaned:
                self._ensure_original_rows(db, run_id, packet, stamp, activate=True)
                db.execute('UPDATE run_originals SET active = 0, released_utc = ? '
                           'WHERE run_id = ? AND active = 1', (stamp, run_id))
                cleaned_packet = {'manifest': packet['manifest'],
                                  'results': {'hours': packet['results']['hours']}}
                db.execute('UPDATE monitoring SET packet = ? WHERE id = ?',
                           (canonical(cleaned_packet), run_id))
                db.execute('''
                    INSERT INTO retention(run_id, issued_utc, released_utc, closed_utc,
                                          created_utc, updated_utc)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        issued_utc = COALESCE(retention.issued_utc, excluded.issued_utc),
                        released_utc = excluded.released_utc,
                        closed_utc = CASE WHEN ? THEN ? ELSE retention.closed_utc END,
                        updated_utc = excluded.updated_utc
                ''', (run_id, stamp if issue_certificate else None, stamp,
                      stamp if close_case else None,
                      packet.get('manifest', {}).get('created_utc', stamp), stamp,
                      1 if close_case else 0, stamp))
                result_packet = cleaned_packet
            else:
                result_packet = packet
                if issue_certificate:
                    raise InputError('EVIDENCE_CLEANED',
                                     '原件已清理，不能重新签发；请复核签发时保存的证据 JSON', 410)

            deleted, retained, retained_runs = [], {}, {}
            for digest in sorted(hashes):
                active_runs = [row[0] for row in db.execute(
                    'SELECT run_id FROM run_originals WHERE sha256 = ? AND active = 1', (digest,))]
                if active_runs:
                    retained[digest] = len(active_runs)
                    retained_runs[digest] = active_runs
                else:
                    db.execute('DELETE FROM originals WHERE sha256 = ?', (digest,))
                    deleted.append(digest)

            retention_row = self._retention_row(db, run_id)
            state = self._retention_dict(run_id, result_packet, retention_row)
            return {**result_packet, 'originals': result_packet.get('originals'), **state,
                    'already_cleaned': already_cleaned, 'hash_count': len(hashes),
                    'deleted_originals': deleted, 'retained_originals': retained,
                    'retained_run_ids': retained_runs,
                    # Shorter aliases keep route/test ergonomics independent of naming.
                    'deleted': deleted, 'retained': retained,
                    'certificate': certificate, 'packet': result_packet}

    # Concise aliases for callers/tests using retention vocabulary.
    def cleanup_run(self, run_id, **kwargs):
        return self.release_originals(run_id, **kwargs)

    def cleanup(self, run_id, **kwargs):
        return self.release_originals(run_id, **kwargs)['packet']

    def clean(self, run_id, **kwargs):
        return self.release_originals(run_id, **kwargs)

    def prune(self, run_id, **kwargs):
        return self.release_originals(run_id, **kwargs)

    @staticmethod
    def _parse_time(value):
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def due_run_ids(self, now=None):
        current = self._parse_time(now) if isinstance(now, str) else now
        current = current or datetime.now(UTC)
        with self.connection() as db:
            rows = db.execute('''
                SELECT m.id, m.packet, r.released_utc, r.due_utc
                FROM monitoring m LEFT JOIN retention r ON r.run_id = m.id
                ORDER BY m.rowid
            ''').fetchall()
        ids = []
        for run_id, raw, released, due in rows:
            if released:
                continue
            packet = json.loads(raw)
            manifest = packet.get('manifest', {}) if isinstance(packet, dict) else {}
            retention_info = packet.get('retention', {}) if isinstance(packet, dict) else {}
            due_value = due or manifest.get('retention_due_utc') or manifest.get('due_utc') \
                or manifest.get('retention_until') or manifest.get('retain_until') \
                or manifest.get('retain_until_utc') or manifest.get('expires_utc') \
                or manifest.get('expires_at') or manifest.get('due_at') \
                or manifest.get('cleanup_after') \
                or (isinstance(retention_info, dict) and (retention_info.get('due_utc')
                                                          or retention_info.get('until')
                                                          or retention_info.get('retention_until')
                                                          or retention_info.get('expires_utc')))
            due_at = self._parse_time(due_value)
            if due_at is None:
                retention_days = os.getenv('MONITOR_RETENTION_DAYS') or os.getenv('RETENTION_DAYS')
                created_at = self._parse_time(manifest.get('created_utc'))
                if retention_days and created_at is not None:
                    try:
                        due_at = created_at + timedelta(days=float(retention_days))
                    except ValueError:
                        due_at = None
            if due_at is None:
                status = (retention_info.get('status') if isinstance(retention_info, dict) else None) \
                    or manifest.get('retention_status')
                if status in {'due', 'expired', 'PENDING_CLEANUP'} \
                        or manifest.get('expired') is True \
                        or (isinstance(retention_info, dict) and retention_info.get('expired') is True):
                    ids.append(run_id)
                continue
            if due_at <= current:
                ids.append(run_id)
        return ids

    def clean_due(self, now=None, **kwargs):
        run_ids = self.due_run_ids(now=now)
        if now is not None:
            kwargs.setdefault('now', now if isinstance(now, str) else None)
        reports = [self.release_originals(run_id, **kwargs) for run_id in run_ids]
        return {'cleaned': reports, 'run_ids': run_ids, 'count': len(reports)}

    def clean_expired(self, **kwargs):
        return self.clean_due(**kwargs)

    def expire_batch(self, run_id, **kwargs):
        return self.release_originals(run_id, **kwargs)

    def clear_originals(self, run_id, **kwargs):
        return self.release_originals(run_id, **kwargs)

    def delete_originals(self, run_id, **kwargs):
        return self.release_originals(run_id, **kwargs)

    def release_retention(self, run_id, **kwargs):
        return self.release_originals(run_id, **kwargs)

    def release(self, run_id, **kwargs):
        return self.release_originals(run_id, **kwargs)

    def prune_expired(self, **kwargs):
        return self.clean_due(**kwargs)

    def history(self, enriched=False):
        with self.connection() as db:
            rows = db.execute('SELECT id, packet FROM monitoring ORDER BY rowid DESC LIMIT 100').fetchall()
            history = []
            for run_id, raw in rows:
                packet = json.loads(raw)
                item = dict(packet.get('manifest', {}))
                if enriched:
                    state_row = self._retention_row(db, run_id)
                    item.update(self._retention_dict(run_id, packet, state_row))
                    hours = packet.get('results', {}).get('hours') if isinstance(packet.get('results'), dict) else None
                    if hours is not None:
                        item['hours'] = hours
                        item['hourly_conclusions'] = hours
                    counts = db.execute('SELECT COUNT(*), COALESCE(SUM(active), 0) '
                                         'FROM run_originals WHERE run_id = ?', (run_id,)).fetchone()
                    item['original_reference_rows'] = counts[0]
                    item['active_original_count'] = counts[1]
                    shared = db.execute('''
                        SELECT COUNT(DISTINCT own.sha256)
                        FROM run_originals own
                        JOIN run_originals other
                          ON own.sha256 = other.sha256 AND other.active = 1 AND other.run_id != own.run_id
                        WHERE own.run_id = ? AND own.active = 1
                    ''', (run_id,)).fetchone()[0]
                    item['shared_original_count'] = shared
                history.append(item)
            return history

    def history_overview(self):
        return self.history(enriched=True)

    def retention_overview(self):
        with self.connection() as db:
            batches = self.history(enriched=True)
            rows = db.execute('''
                SELECT ro.sha256,
                       SUM(ro.active) AS active_references,
                       COUNT(ro.run_id) AS reference_rows,
                       GROUP_CONCAT(CASE WHEN ro.active = 1 THEN ro.run_id END) AS active_run_ids,
                       EXISTS(SELECT 1 FROM originals o WHERE o.sha256 = ro.sha256) AS stored
                FROM run_originals ro
                GROUP BY ro.sha256
                ORDER BY ro.sha256
            ''').fetchall()
            originals = [{'sha256': digest, 'references': active or 0,
                          'reference_rows': total_rows,
                          'run_ids': [value for value in (run_ids or '').split(',') if value],
                          'stored': bool(stored)}
                         for digest, active, total_rows, run_ids, stored in rows]
            shared = [row for row in originals if row['references'] > 1]
        return {'batches': batches, 'runs': batches, 'originals': originals,
                'references': originals, 'original_references': originals,
                'shared_originals': shared}


def clean_expired(archive, **kwargs):
    return archive.clean_expired(**kwargs)


def clean_due(archive, **kwargs):
    return archive.clean_due(**kwargs)


def release_originals(archive, run_id, **kwargs):
    return archive.release_originals(run_id, **kwargs)
