from hashlib import sha256

from app.archive.manifest import canonical
from app.errors import InputError
from app.monitor.source import digests

CERTIFICATE_KEYS = ('manifest', 'originals', 'results')
RESERVED_CLAIMS = {'run_id', 'certificate', 'minute_balance', 'sha256', 'valid'}


def certificate_core(packet):
    """Return the signed core while tolerating an already-issued attachment."""
    if not isinstance(packet, dict):
        raise InputError('EVIDENCE_PACKET', '证据单必须是 JSON 对象', 422)
    if all(key in packet for key in CERTIFICATE_KEYS):
        core = {key: packet[key] for key in CERTIFICATE_KEYS}
    elif isinstance(packet.get('certificate'), dict):
        return certificate_core(packet['certificate'])
    else:
        raise InputError('EVIDENCE_INCOMPLETE', '证据单缺少 manifest、originals 或 results', 410)
    if not isinstance(core.get('originals'), dict) or not isinstance(core.get('manifest'), dict) \
            or not isinstance(core.get('results'), dict):
        raise InputError('EVIDENCE_INCOMPLETE', '证据单结构不完整', 410)
    return core


def issue(packet):
    core = certificate_core(packet)
    try:
        expected = core['manifest']['sha256']
    except (KeyError, TypeError):
        raise InputError('EVIDENCE_MISMATCH', '原件摘要与留档不符', 409) from None
    if digests(core['originals']) != expected:
        raise InputError('EVIDENCE_MISMATCH', '原件摘要与留档不符', 409)
    totals = core['results']['hours']['totals']
    delta = totals['scheduled'] - totals['valid'] - totals['exempt'] - totals['missing']
    if delta:
        raise InputError('EVIDENCE_UNBALANCED', '分钟勾稽尚未平衡', 409)
    digest = sha256(canonical(core).encode()).hexdigest()
    claims = {'run_id': core['manifest']['run_id'], 'certificate': '监测证据单',
              'minute_balance': delta, 'sha256': digest}
    return claims | core


def verify(packet):
    """Validate an exported JSON attachment after server-side cleanup."""
    issued = issue(packet)
    provided = packet.get('sha256') if isinstance(packet, dict) else None
    if isinstance(packet.get('certificate'), dict) and provided is None:
        provided = packet['certificate'].get('sha256')
    if provided is not None and provided != issued['sha256']:
        raise InputError('EVIDENCE_DIGEST_MISMATCH', '证据单内容摘要不一致', 409)
    return {'valid': True, **issued}


check = verify
verify_evidence = verify
