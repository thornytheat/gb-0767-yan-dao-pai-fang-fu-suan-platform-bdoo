from flask import Blueprint, Response, jsonify, redirect, render_template, request, url_for

from app.api import archive
from app.archive.certificate import issue
from app.archive.manifest import batch_key, canonical
from app.archive.retention import requested
from app.config import VERSION
from app.errors import InputError
from app.monitor.inspection import Inspection
from app.monitor.source import sample
from app.reasons import LABELS

PAGES = {'intake': '分钟原件采信', 'oxygen': '干基与参比氧折算', 'validity': '分钟有效性裁决',
         'hours': '小时汇总复算', 'reports': '报送差异对账', 'runs': '监测留档复跑', 'evidence': '监测证据签发'}
desk = Blueprint('desk', __name__)


def _page_context(page, run_id, name):
    history, certificate, cleaned, retention = [], None, False, None
    if run_id:
        packet = archive().require(run_id)
        results, meta = packet['results'], packet['manifest']
        cleaned = not packet.get('originals')
        history_items = archive().history(enriched=True)
        retention = next((item for item in history_items if item['run_id'] == run_id), None)
        if page == 'evidence' and not cleaned:
            certificate = issue(packet)
        if page == 'runs':
            history = history_items
    else:
        sources = sample(name)
        audit = Inspection(sources)
        results = audit.results()
        key, _ = batch_key(sources)
        meta = {'run_id': key, 'rule': VERSION, 'source': name, 'parameters': vars(audit.window)}
    if page == 'runs' and not history:
        history = archive().history()
    return results, meta, history, certificate, cleaned, retention


@desk.get('/archive')
@desk.get('/monitor/retention')
@desk.get('/monitor/archive')
def retention_page():
    return show('runs')


@desk.get('/')
@desk.get('/monitor/<page>')
def show(page='home'):
    if page != 'home' and page not in PAGES:
        raise InputError('STAGE_UNKNOWN', '找不到监测工序', 404)
    name, run_id = request.args.get('sample', 'dirty'), request.args.get('run')
    results, meta, history, certificate, cleaned, retention = _page_context(page, run_id, name)
    overview = archive().retention_overview() if page == 'runs' else {'originals': []}
    if page == 'runs':
        history = overview['batches']
    locked = cleaned and page not in {'hours', 'runs', 'evidence'}
    chart = [row for row in results['hours']['rows'] if row['mg_nm3'] is not None]
    ceiling = max((float(row['mg_nm3']) for row in chart), default=0)
    issue_rows = [] if cleaned else [
        r for r in results['validity']['rows'] if r['state'] == 'missing' or r['late']
    ]
    return render_template('base.html', page=page, pages=PAGES, name=name, run_id=run_id, history=history,
                           certificate=certificate, cleaned=cleaned, retention=retention, locked=locked,
                           original_rows=overview['originals'],
                           result=results, meta=meta, ceiling=ceiling, labels=LABELS,
                           issue_rows=issue_rows)


@desk.post('/monitor/runs/<run_id>/cleanup')
def cleanup_form(run_id):
    want_certificate = requested(request.form.get('issue', request.form.get('certificate', False)))
    report = archive().release_originals(
        run_id, issue_certificate=want_certificate,
        close_case=requested(request.form.get('close_case', False)))
    certificate = report.get('certificate')
    if certificate is not None and requested(request.form.get('download', '1')):
        return Response(canonical(certificate), mimetype='application/json', headers={
            'Content-Disposition': f'attachment; filename="monitor-{run_id}.json"'})
    return redirect(url_for('desk.show', page='runs', cleaned=1))


@desk.post('/monitor/retention/clean-due')
def clean_due_form():
    from app.archive.retention import cleanup_many
    want_certificate = requested(request.form.get('issue', request.form.get('certificate', False)))
    result = cleanup_many(archive(), {'issue': want_certificate})
    if want_certificate:
        return jsonify(result)
    return redirect(url_for('desk.show', page='runs', cleaned=1))
