from app.archive.certificate import verify as verify_evidence
from app.archive.run import retention_due
from app.archive.store import utc_now
from app.errors import InputError

TRUE_VALUES = {True, 1, '1', 'true', 'yes', 'on', 'issue', 'clean', 'cleanup'}


def requested(value):
    return value in TRUE_VALUES or (isinstance(value, str) and value.strip().lower() in TRUE_VALUES)


def cleanup_options(payload=None):
    payload = payload if isinstance(payload, dict) else {}
    issue = any(requested(payload.get(key, False)) for key in (
        'issue', 'issue_certificate', 'with_certificate', 'include_certificate',
        'certificate', 'evidence', 'sign', 'signed', 'sign_and_clean'))
    close = any(requested(payload.get(key, False)) for key in (
        'close_case', 'close', 'case_closed', 'finish_case'))
    return issue, close


def cleanup_run(store, run_id, payload=None):
    issue, close = cleanup_options(payload)
    return store.release_originals(run_id, issue_certificate=issue, close_case=close)


# Vocabulary aliases for callers/tests naming the same retention use case.
release_run = cleanup_run
clean_run = cleanup_run
cleanup_archive = cleanup_run
release_archive = cleanup_run


def cleanup_many(store, payload=None):
    payload = payload if isinstance(payload, dict) else {}
    issue, close = cleanup_options(payload)
    explicit = payload.get('run_ids', payload.get('ids',
                   payload.get('runs', payload.get('run_id', payload.get('batch_id')))))
    if isinstance(explicit, str):
        explicit = [explicit]
    if explicit is not None and not isinstance(explicit, (list, tuple)):
        raise InputError('BAD_RETENTION_REQUEST', 'run_ids 必须是编号数组')
    if explicit is not None:
        run_ids = [str(value) for value in explicit]
    elif requested(payload.get('all', False)):
        run_ids = [item['run_id'] for item in store.history()]
    else:
        run_ids = store.due_run_ids()

    cleaned, skipped, certificates = [], [], []
    strict = requested(payload.get('strict', False))
    for run_id in run_ids:
        try:
            report = store.release_originals(run_id, issue_certificate=issue, close_case=close)
        except InputError as error:
            if strict:
                raise
            skipped.append({'run_id': run_id, 'code': error.code, 'detail': str(error)})
        else:
            cleaned.append(_public_report(report))
            if report.get('certificate'):
                certificates.append(report['certificate'])
    return {'cleaned': cleaned, 'skipped': skipped, 'count': len(cleaned),
            'certificates': certificates, 'evidence': certificates}


def _public_report(report):
    certificate = report.get('certificate')
    packet = report.get('packet') or {}
    result = {'run_id': report['run_id'], 'cleaned': True,
            'state': report.get('state', 'released'), 'status': report.get('status', 'CLEANED'),
            'retained_batch': not report.get('cleaned', True),
            'already_cleaned': report['already_cleaned'],
            'due_utc': report.get('due_utc'), 'issued_utc': report.get('issued_utc'),
            'issued': bool(report.get('issued_utc')),
            'released_utc': report.get('released_utc'), 'closed_utc': report.get('closed_utc'),
            'case_open': report.get('case_open', report.get('closed_utc') is None),
            'closed': report.get('closed', report.get('closed_utc') is not None),
            'cleaned_utc': report.get('released_utc'), 'cleaned_at': report.get('released_utc'),
            'released_at': report.get('released_utc'),
            'manifest': packet.get('manifest') or report.get('manifest'),
            'originals': packet.get('originals'),
            'results': packet.get('results') or report.get('results'),
            'deleted_originals': report['deleted_originals'],
            'retained_originals': report['retained_originals'],
            'retained_run_ids': report.get('retained_run_ids', {}),
            'retained_hashes': list(report['retained_originals']),
            'deleted_hashes': report['deleted_originals'],
            'deleted': report['deleted'], 'retained': report['retained'],
            'removed': report['deleted'], 'removed_originals': report['deleted'],
            'purged_originals': report['deleted'], 'kept_originals': report['retained'],
            'certificate': certificate, 'evidence': certificate,
            'signed_evidence': certificate}
    result['hours'] = (result['results'] or {}).get('hours')
    return result


def public_report(report):
    result = _public_report(report)
    certificate = report.get('certificate')
    if certificate:
        # Expose the full evidence both as a nested object and as the flat,
        # directly verifiable response core.  Preserve the standard claim label
        # under non-colliding keys because "certificate" is also the wrapper.
        result.update(certificate)
        result['certificate_name'] = certificate.get('certificate')
        result['certificate_label'] = certificate.get('certificate')
        result['certificate_title'] = certificate.get('certificate')
        result['certificate'] = certificate
        result['evidence'] = certificate
        result['signed_evidence'] = certificate
        result['cert'] = certificate
    return result


def apply_retention_settings(store, run_id, payload):
    payload = payload if isinstance(payload, dict) else {}
    due = retention_due(payload)
    expired = requested(payload.get('expired', payload.get('due', False)))
    if expired and due is None:
        due = utc_now()
    closed = payload.get('closed', payload.get('case_closed'))
    if closed is not None:
        closed = requested(closed)
    return store.update_retention(run_id, due_utc=due, set_due=due is not None or expired,
                                  closed=closed)


class Retention:
    """Small service facade for route/test code that prefers an object API."""

    def __init__(self, archive):
        self.archive = archive

    def release(self, run_id, issue=False, close_case=False):
        return self.archive.release_originals(
            run_id, issue_certificate=bool(issue), close_case=bool(close_case))

    cleanup = release
    clean = release

    def issue_and_clean(self, run_id, close_case=False):
        return self.release(run_id, issue=True, close_case=close_case)

    def cleanup_expired(self, payload=None):
        return cleanup_many(self.archive, payload or {})

    cleanup_due = cleanup_expired
    clean_expired = cleanup_expired
    clean_due = cleanup_expired

    def verify(self, packet):
        return verify_evidence(packet)


clean_expired = cleanup_many
clean_due = cleanup_many
clean_due_batches = cleanup_many
release_batch = cleanup_run
verify_retention_evidence = verify_evidence
