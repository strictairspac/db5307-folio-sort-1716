from urllib.parse import quote

from starlette.responses import Response

from src.config.settings import engine
from src.models.tables import evidence
from src.services import orders
from src.services.workflow import found
from src.utils.encoding import canonical


def download(identity):
    with engine.connect() as conn:
        data = found(conn, evidence, identity)
    return Response(canonical(data), media_type='application/json', headers={
        'Content-Disposition': f'attachment; filename="evidence-{identity}.json"'})


def download_order(key):
    data, meta = orders.export_order(key)
    filename = f'order-{meta["batch_no"]}.jsonl'
    return Response(data, media_type='application/x-ndjson', headers={
        'Content-Disposition': f'attachment; filename="{quote(filename)}"; filename*=UTF-8\'\'{quote(filename)}',
        'X-Order-Checksum': meta['checksum'],
        'X-Order-Records': str(meta['records']),
        'X-Order-Segments': str(meta['segments'] if meta['segments'] is not None else 1),
        'X-Order-State': meta['state'],
        'X-Content-Type-Options': 'nosniff'})
