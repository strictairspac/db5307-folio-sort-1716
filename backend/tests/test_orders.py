import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

from src.config.settings import engine
from src.main import app
from src.models.tables import cases, evidence, orders, runs, segments
from src.services import domain, workflow
from src.services.orders import _bytes, _line, _sha


@pytest.fixture()
def client():
    with TestClient(app) as testing:
        with engine.begin() as conn:
            conn.execute(delete(segments))
            conn.execute(delete(orders))
            conn.execute(delete(evidence))
            conn.execute(delete(runs))
            conn.execute(delete(cases))
        yield testing


def auth(client, username='operator'):
    token = client.post('/login', json={'username': username, 'password': 'Demo-Walkthrough-2026'}).json()['token']
    return {'Authorization': f'Bearer {token}'}


def completed_run(client, headers, payload=None, title='批次'):
    payload = payload or client.get('/spec', headers=headers).json()['sample']
    case = client.post('/cases', headers=headers, json={'title': title, 'payload': payload}).json()
    run = client.post(f'/cases/{case["id"]}/runs', headers=headers).json()
    job = workflow.claim()
    assert job[0] == run['id']
    workflow.finish(job[0], job[1], result=domain.solve(job[2], engine))
    evidence_id = client.get('/evidence', headers=headers).json()[0]['id']
    return f'evidence:{evidence_id}', evidence_id


def all_pages(client, headers, key, size=2):
    page = client.get(f'/orders/{key}/page', headers=headers, params={'size': size}).json()
    rows = list(page['rows'])
    seen = [page['start_rank']]
    while page['next_cursor']:
        page = client.get(f'/orders/{key}/page', headers=headers,
                          params={'cursor': page['next_cursor']}).json()
        seen.append(page['start_rank'])
        rows.extend(page['rows'])
    return rows, seen


def test_archive_pages_cover_each_page_only(client):
    headers = auth(client)
    key, _ = completed_run(client, headers)
    rows, starts = all_pages(client, headers, key, size=2)
    assert [row['上架顺位'] for row in rows] == list(range(1, 10))
    assert starts == [1, 3, 5, 7, 9]
    assert len({row['馆藏件号'] for row in rows}) == 9

    first = client.get(f'/orders/{key}/page', headers=headers, params={'size': 2}).json()
    # 2 件一段时，2 件的一页恰好覆盖 1 段。
    assert [item['ordinal'] for item in first['segments_read']] == [1]
    middle = client.get(f'/orders/{key}/page', headers=headers, params={'size': 1, 'cursor': first['next_cursor']}).json()
    assert len(middle['segments_read']) == 1


def test_archive_failure_leaves_no_partial_active_order(client, monkeypatch):
    from sqlalchemy import select as sa_select

    import src.services.orders as order_service

    headers = auth(client)
    payload = client.get('/spec', headers=headers).json()['sample']
    result = domain.solve(payload, engine)

    class Boom(Exception):
        pass

    def explode(*args, **kwargs):
        raise Boom()

    monkeypatch.setattr(order_service, '_insert_segment', explode)
    with pytest.raises(Boom):
        with engine.begin() as conn:
            order_service.archive_report(conn, result, payload['batch_no'], payload, 'evidence:99')
    # 段未写完的存档只能是 staging，绝不可见为 active；翻页与导出仍回退旧数据。
    with engine.connect() as conn:
        states = conn.execute(sa_select(orders.c.state)).scalars().all()
        assert 'active' not in states
        assert conn.execute(sa_select(segments.c.id)).first() is None


def test_evidence_list_omits_inline_order(client):
    headers = auth(client)
    key, _ = completed_run(client, headers)
    listed = client.get('/evidence', headers=headers).json()[0]
    assert all(section['title'] != '上架顺序单' for section in listed['report']['sections'])
    assert 'order' in listed['report']
    # 导出证据仍保留原始完整报告。
    downloaded = client.get(f'/evidence/{listed["id"]}/download', headers=headers).json()
    assert any(section['title'] == '上架顺序单' for section in downloaded['report']['sections'])


def test_export_preserves_format_and_checksum(client):
    headers = auth(client)
    key, _ = completed_run(client, headers)
    response = client.get(f'/orders/{key}/export', headers=headers)
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('application/x-ndjson')
    data = response.content
    assert data.endswith(b'\n')
    assert data.count(b'\n') == 9
    first = json.loads(data.splitlines()[0])
    assert first == [5, 'BX-05', 'GJ-2025-8', '山川图考']
    assert response.headers['X-Order-Checksum'] == hashlib.sha256(data).hexdigest()
    assert response.headers['X-Order-Records'] == '9'


def test_bad_segment_refuses_whole_export_and_page(client):
    headers = auth(client)
    key, _ = completed_run(client, headers)
    with engine.begin() as conn:
        conn.execute(update(segments).where(segments.c.ordinal == 3).values(lines=['[0,"x","GJ-2026-1","坏"]\n']))
    assert client.get(f'/orders/{key}/export', headers=headers).status_code == 409
    page = client.get(f'/orders/{key}/page', headers=headers, params={'size': 20})
    assert page.status_code == 409
    # 未覆盖坏段的页仍可读。
    assert client.get(f'/orders/{key}/page', headers=headers, params={'size': 2}).status_code == 200


def test_cursor_binds_to_single_order(client):
    headers = auth(client)
    key, _ = completed_run(client, headers)
    other_key, _ = completed_run(client, headers, title='另一批')
    cursor = client.get(f'/orders/{key}/page', headers=headers, params={'size': 2}).json()['next_cursor']
    assert client.get(f'/orders/{other_key}/page', headers=headers,
                      params={'cursor': cursor}).status_code == 422
    assert client.get(f'/orders/{key}/page', headers=headers,
                      params={'cursor': cursor + 'x'}).status_code == 422


def test_legacy_migration_resume_is_idempotent(client):
    headers = auth(client)
    key, evidence_id = completed_run(client, headers)
    with engine.begin() as conn:
        conn.execute(delete(segments))
        conn.execute(delete(orders))

    # 迁移前发出的翻页凭据。
    legacy = client.get(f'/orders/{key}/page', headers=headers, params={'size': 2}).json()
    assert legacy['source'] == 'legacy'
    cursor = legacy['next_cursor']
    legacy_next = client.get(f'/orders/{key}/page', headers=headers,
                             params={'cursor': cursor}).json()

    # 模拟中断：只留下第 1 个段。
    # 列表不再内联整份顺序单；从库中取报告模拟此前的存库状态。
    with engine.connect() as conn:
        report = conn.execute(select(evidence.c.report).where(
            evidence.c.id == evidence_id)).scalar_one()
    rows = report['sections'][0]['rows']
    lines = [_line(row) for row in rows]
    with engine.begin() as conn:
        conn.execute(orders.insert().values(
            key=key, batch_no='WX-20261121-AM', checksum='pending', records=9, segments=5,
            format='jsonl-v1', state='staging', source='migration', segment_items=2,
            evidence_id=evidence_id, created_at='2026-11-21T00:00:00+00:00'))
        order_id = conn.execute(select(orders.c.id)).scalar_one()
        conn.execute(segments.insert().values(
            order_id=order_id, ordinal=1, start_rank=1, end_rank=2, lines=lines[:2],
            checksum=_sha(_bytes(lines[:2]))))

    reviewer = auth(client, 'reviewer')
    assert client.post(f'/evidence/{evidence_id}/review', headers=reviewer,
                       json={'note': '迁移期间照常签署'}).status_code == 200

    result = client.post('/orders/migrate', headers=headers, params={'batch_size': 10}).json()
    assert result['results'] == [{'key': key, 'status': 'migrated', 'segments': 5}]
    assert client.post('/orders/migrate', headers=headers, params={'batch_size': 10}).json()['processed'] == 0

    with engine.connect() as conn:
        assert conn.execute(select(segments.c.ordinal).where(
            segments.c.order_id == order_id).order_by(segments.c.ordinal)).scalars().all() == [1, 2, 3, 4, 5]
        assert conn.execute(select(orders.c.state).where(orders.c.id == order_id)).scalar_one() == 'active'

    # 切换前的凭据继续读同一份单。
    after = client.get(f'/orders/{key}/page', headers=headers, params={'cursor': cursor}).json()
    assert after['source'] == 'archive'
    assert after['rows'] == legacy_next['rows']
    reviewed = client.get('/evidence', headers=headers).json()[0]
    assert reviewed['review_note'] == '迁移期间照常签署'


def test_new_orders_write_during_pending_migration(client):
    headers = auth(client)
    old_key, _ = completed_run(client, headers, title='旧单')
    with engine.begin() as conn:
        conn.execute(delete(segments))
        conn.execute(delete(orders))
    new_key, _ = completed_run(client, headers, title='新单')
    assert client.get(f'/orders/{new_key}', headers=headers).json()['state'] == 'active'
    result = client.post('/orders/migrate', headers=headers, params={'batch_size': 10}).json()
    assert {item['key'] for item in result['results']} == {old_key}
    assert client.get(f'/orders/{old_key}', headers=headers).json()['state'] == 'active'


def test_failed_migration_leaves_legacy_readable(client):
    headers = auth(client)
    key, evidence_id = completed_run(client, headers)
    with engine.begin() as conn:
        conn.execute(delete(segments))
        conn.execute(delete(orders))
        report = conn.execute(select(evidence.c.report).where(evidence.c.id == evidence_id)).scalar_one()
        broken = json.loads(json.dumps(report))
        broken['sections'][0]['rows'][0]['上架顺位'] = 99
        conn.execute(update(evidence).where(evidence.c.id == evidence_id).values(report=broken))
    result = client.post('/orders/migrate', headers=headers).json()
    assert result['results'][0]['status'] == 'failed'
    with engine.connect() as conn:
        assert conn.execute(select(orders.c.state)).scalars().all() == []
    assert client.get(f'/orders/{key}/page', headers=headers).status_code in (200, 409)
