import json

from flask import Blueprint, current_app, jsonify, request

from app.archive.certificate import verify
from app.archive.retention import (apply_retention_settings, cleanup_many, cleanup_run,
                                   public_report, requested)
from app.archive.run import archive_run
from app.errors import InputError
from app.monitor.inspection import STAGES, Inspection
from app.monitor.source import sample

api = Blueprint('api', __name__, url_prefix='/api')


def archive():
    return current_app.extensions['monitor_archive']


def body_data():
    payload = request.get_json(silent=True)
    if payload is not None:
        return payload
    if request.form:
        return request.form.to_dict(flat=True)
    return {}


def flag(name, payload=None, default=False):
    payload = payload if isinstance(payload, dict) else {}
    if name in payload:
        return requested(payload[name])
    if name in request.args:
        return requested(request.args[name])
    return default


@api.get('/samples/<name>/<stage>')
def preview(name, stage):
    if stage not in STAGES:
        raise InputError('STAGE_UNKNOWN', '找不到监测工序', 404)
    return jsonify(getattr(Inspection(sample(name)), stage))


@api.post('/runs')
def create_run():
    packet, reused = archive_run(archive(), request.get_json(silent=True))
    hours = packet['results']['hours']
    reports = packet['results'].get('reports', {})
    return jsonify(run_id=packet['manifest']['run_id'], reused=reused, manifest=packet['manifest'],
                   cleaned=not packet.get('originals'),
                   totals=hours['totals'], deviations=reports.get('deviations'))


@api.get('/runs')
def runs():
    return jsonify(runs=archive().history(enriched=True))


@api.get('/retention')
@api.get('/runs/retention')
def retention_overview():
    return jsonify(archive().retention_overview())


@api.post('/cleanup')
@api.post('/batches/cleanup')
@api.post('/archives/cleanup')
@api.post('/retention')
@api.post('/retention/release')
@api.post('/retention/clean')
@api.post('/retention/cleanup')
@api.post('/retention/clean-due')
@api.post('/retention/cleanup-expired')
@api.post('/runs/cleanup')
@api.post('/runs/clean-due')
@api.post('/runs/cleanup-expired')
@api.post('/runs/expire-due')
@api.post('/runs/purge-expired')
def cleanup_due():
    return jsonify(cleanup_many(archive(), body_data()))


@api.post('/retention/cleanup/<run_id>')
@api.post('/retention/clean/<run_id>')
@api.post('/export/<run_id>/cleanup')
@api.post('/exports/<run_id>/cleanup')
@api.post('/batches/<run_id>/cleanup')
@api.post('/batches/<run_id>/clean')
@api.post('/archives/<run_id>/cleanup')
@api.post('/retention/<run_id>/cleanup')
@api.post('/retention/<run_id>/clean')
@api.post('/cleanup/<run_id>')
@api.post('/clean/<run_id>')
@api.post('/runs/<run_id>/cleanup')
@api.post('/runs/<run_id>/clean')
@api.post('/runs/<run_id>/release')
@api.post('/runs/<run_id>/prune')
@api.post('/runs/<run_id>/purge-originals')
@api.post('/runs/<run_id>/clear-originals')
@api.post('/runs/<run_id>/delete-originals')
@api.post('/runs/<run_id>/originals/cleanup')
@api.post('/runs/<run_id>/issue-clean')
@api.post('/runs/<run_id>/sign-and-clean')
@api.post('/runs/<run_id>/delete')
@api.post('/runs/<run_id>/remove')
@api.post('/runs/<run_id>/destroy')
@api.post('/runs/<run_id>/retention/release')
@api.post('/runs/<run_id>/retention/clean')
@api.post('/runs/<run_id>/retention/cleanup')
@api.post('/runs/<run_id>/originals/release')
@api.post('/archive/runs/<run_id>/cleanup')
@api.post('/archive/cleanup/<run_id>')
@api.put('/runs/<run_id>/cleanup')
@api.post('/archive/<run_id>/cleanup')
@api.post('/archive/<run_id>/clean')
@api.post('/retention/<run_id>/release')
@api.post('/retention/release/<run_id>')
@api.delete('/runs/<run_id>/originals')
@api.delete('/runs/<run_id>/originals/delete')
def cleanup(run_id):
    payload = body_data()
    # Query-string flags make simple HTML links/forms and curl calls possible.
    merged = dict(request.args) | (payload if isinstance(payload, dict) else {})
    report = cleanup_run(archive(), run_id, merged)
    return jsonify(public_report(report))


@api.post('/runs/<run_id>/retention')
@api.patch('/runs/<run_id>/retention')
@api.put('/runs/<run_id>/retention')
@api.post('/retention/runs/<run_id>')
def update_retention(run_id):
    payload = body_data()
    action = str(payload.get('action', '')).lower()
    should_release = action in {'release', 'cleanup', 'clean', 'purge', 'delete_originals'} \
        or payload.get('retained') is False or flag('release', payload) or flag('cleanup', payload)
    if should_release:
        return jsonify(public_report(cleanup_run(archive(), run_id, payload)))
    return jsonify(apply_retention_settings(archive(), run_id, payload))


@api.post('/runs/<run_id>/close')
def close_case(run_id):
    return jsonify(archive().update_retention(run_id, closed=True))


@api.post('/runs/<run_id>/reopen')
def reopen_case(run_id):
    return jsonify(archive().update_retention(run_id, closed=False))


@api.get('/batches/<run_id>')
@api.get('/archives/<run_id>')
@api.get('/runs/<run_id>')
def get_run(run_id):
    packet = archive().require(run_id)
    overview = next((item for item in archive().history(enriched=True) if item['run_id'] == run_id), {})
    return jsonify(packet | {'originals': packet.get('originals'),
                             'cleaned': overview.get('cleaned', not packet.get('originals')),
                             'hours': packet['results'].get('hours'),
                             'retention': {key: overview.get(key) for key in
                                           ('due_utc', 'issued_utc', 'released_utc', 'closed_utc',
                                            'case_open', 'shared_original_count',
                                            'active_original_count')}})


@api.post('/runs/<run_id>')
@api.patch('/runs/<run_id>')
@api.put('/runs/<run_id>')
def run_action(run_id):
    payload = body_data()
    action = str(payload.get('action', '')).lower() if isinstance(payload, dict) else ''
    if action in {'cleanup', 'clean', 'release', 'purge', 'delete_originals'} \
            or payload.get('_method', '').upper() == 'DELETE':
        return cleanup(run_id)
    if action in {'issue', 'certificate', 'sign'}:
        return issue_run(run_id)
    if action in {'close', 'close_case'}:
        return close_case(run_id)
    if action in {'reopen'}:
        return reopen_case(run_id)
    return update_retention(run_id)


@api.get('/runs/<run_id>/<stage>')
def snapshot(run_id, stage):
    if stage not in STAGES:
        raise InputError('STAGE_UNKNOWN', '找不到监测工序', 404)
    packet = archive().require(run_id)
    if stage not in packet['results']:
        raise InputError('ARCHIVE_CLEANED', '该批次原件与明细已清理，仅保留小时结论', 410)
    return jsonify(packet['results'][stage])


@api.post('/runs/<run_id>/expire')
def expire_run(run_id):
    return jsonify(apply_retention_settings(archive(), run_id, {'expired': True}))


@api.post('/runs/<run_id>/export')
@api.post('/runs/<run_id>/issue')
@api.post('/runs/<run_id>/certificate')
def issue_run(run_id):
    payload = body_data()
    should_clean = any(flag(name, payload) for name in
                       ('cleanup', 'clean', 'release', 'purge', 'remove_originals'))
    if should_clean:
        report = archive().release_originals(run_id, issue_certificate=True)
        return jsonify(report['certificate'] | {'cleanup': {
            'cleaned': True, 'released_utc': report['released_utc'],
            'deleted_originals': report['deleted_originals'],
            'retained_originals': report['retained_originals']}})
    from app.archive.certificate import issue
    return jsonify(verify_issue(archive().require(run_id)))


@api.get('/retention/<run_id>')
@api.get('/runs/<run_id>/retention-state')
@api.get('/runs/<run_id>/originals')
def retention_state(run_id):
    packet = archive().require(run_id)
    item = next((row for row in archive().history(enriched=True) if row['run_id'] == run_id), {})
    originals = [row for row in archive().retention_overview()['originals']
                 if run_id in row['run_ids']]
    return jsonify(item | {'has_originals': bool(packet.get('originals')), 'originals': originals})


@api.post('/evidence')
@api.post('/evidence/verify')
@api.post('/evidence/check')
@api.post('/certificate/verify')
@api.post('/certificates/verify')
@api.post('/export/verify')
def verify_evidence():
    payload = request.get_json(silent=True)
    if payload is None and request.form.get('evidence'):
        try:
            payload = json.loads(request.form['evidence'])
        except json.JSONDecodeError:
            raise InputError('BAD_EVIDENCE', '证据文本不是有效 JSON') from None
    if payload is None and request.files.get('file'):
        try:
            payload = json.load(request.files['file'].stream)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise InputError('BAD_EVIDENCE', '证据文件不是有效 JSON') from None
    return jsonify(verify(payload))


@api.get('/runs/<run_id>/export')
@api.get('/export/<run_id>')
def export(run_id):
    packet = archive().require(run_id)
    response = jsonify(verify_issue(packet))
    response.headers['Content-Disposition'] = f'attachment; filename="monitor-{run_id}.json"'
    return response


@api.post('/export/<run_id>')
def export_or_clean(run_id):
    payload = body_data()
    if any(flag(name, payload) for name in ('cleanup', 'clean', 'release', 'issue_and_clean')):
        report = archive().release_originals(run_id, issue_certificate=True)
        response = jsonify(report['certificate'])
        response.headers['X-Retention-Cleaned'] = '1'
        return response
    return export(run_id)


def verify_issue(packet):
    # Local indirection keeps the route readable and maps cleaned archives to 410.
    from app.archive.certificate import issue
    try:
        return issue(packet)
    except InputError as error:
        if error.code == 'EVIDENCE_INCOMPLETE':
            raise InputError('EVIDENCE_CLEANED',
                             '原件已清理，不能重新签发；请复核签发时下载的证据 JSON', 410) from None
        raise
