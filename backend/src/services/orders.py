"""馆藏顺序单的整份存档、分段翻页、校验导出与旧数据迁移。

顺序单以原始 JSONL 格式（每行 ``[原登记顺位, 馆藏件号, 登录号, 题名]``）存档。
一份顺序单被切成若干连续段；翻页只读取覆盖该页的段。导出按原始字节拼接全部段，
写入校验码，并在任一坏段、段范围断裂或整单校验不符时拒绝整单导出。

旧证据里的顺序单按 ``evidence:<id>`` 作为稳定键迁移到新表。迁移先以 staging 状态
幂等补写缺失段，再整单校验通过后原子切换为 active；期间新单写入与旧单签署互不阻塞。
翻页凭据携带稳定键、偏移和整单校验码，切换前后都解析到同一份单。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re

from fastapi import HTTPException
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError

from src.config.settings import AUTH_KEY, engine
from src.models.tables import cases, evidence, orders, runs, segments
from src.utils.time import utc_time

FORMAT = 'jsonl-v1'
DEFAULT_SEGMENT_ITEMS = 64
MAX_PAGE_SIZE = 200
ORDER_SECTION = '上架顺序单'
_ROW_KEYS = ('上架顺位', '馆藏件号', '馆藏登录号', '题名', '原登记顺位')
_EVIDENCE_KEY = re.compile(r'evidence:([1-9][0-9]*)')


class OrderCorrupt(RuntimeError):
    """顺序单任一段无法通过校验。"""


def evidence_key(identity: int) -> str:
    return f'evidence:{identity}'


def _line(row: dict) -> str:
    return json.dumps(
        [row['原登记顺位'], row['馆藏件号'], row['馆藏登录号'], row['题名']],
        ensure_ascii=False,
        separators=(',', ':'),
    )


def _bytes(lines: list[str]) -> bytes:
    return ('\n'.join(lines) + '\n').encode('utf-8') if lines else b''


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rows_from_report(report: object) -> list[dict]:
    if not isinstance(report, dict):
        raise OrderCorrupt('证据报告不是对象，无法读取顺序单')
    for section in report.get('sections', []):
        if isinstance(section, dict) and section.get('title') == ORDER_SECTION:
            rows = section.get('rows')
            if not isinstance(rows, list):
                raise OrderCorrupt('顺序单段落缺少行数据')
            checked = []
            for index, row in enumerate(rows, start=1):
                if not isinstance(row, dict) or any(key not in row for key in _ROW_KEYS):
                    raise OrderCorrupt(f'顺序单第 {index} 行字段不完整')
                if row['上架顺位'] != index:
                    raise OrderCorrupt(f'顺序单第 {index} 行顺位不连续')
                checked.append({key: row[key] for key in _ROW_KEYS})
            return checked
    raise OrderCorrupt('证据报告不含上架顺序单')


def _line_rows(lines: list[str], start_rank: int) -> list[dict]:
    parsed = []
    for offset, line in enumerate(lines):
        try:
            position, item_id, accession_no, title = json.loads(line)
        except (ValueError, TypeError) as exc:
            raise OrderCorrupt(f'第 {start_rank + offset} 件顺序单行无法解析') from exc
        if not isinstance(position, int):
            raise OrderCorrupt(f'第 {start_rank + offset} 件顺序单行缺少原登记顺位')
        parsed.append({
            '上架顺位': start_rank + offset,
            '馆藏件号': item_id,
            '馆藏登录号': accession_no,
            '题名': title,
            '原登记顺位': position,
        })
    return parsed


def segment_items_for(payload: object) -> int:
    if isinstance(payload, dict):
        value = payload.get('segment_items', DEFAULT_SEGMENT_ITEMS)
        if type(value) is int and 1 <= value <= 10000:
            return value
    return DEFAULT_SEGMENT_ITEMS


def _plan(rows: list[dict], segment_items: int) -> list[dict]:
    lines = [_line(row) for row in rows]
    planned = []
    for offset in range(0, len(rows), segment_items):
        chunk = lines[offset : offset + segment_items]
        planned.append({
            'ordinal': len(planned) + 1,
            'start_rank': rows[offset]['上架顺位'],
            'end_rank': rows[offset + len(chunk) - 1]['上架顺位'],
            'lines': chunk,
            'checksum': _sha(_bytes(chunk)),
        })
    return planned


def _whole(planned: list[dict]) -> tuple[str, int]:
    return _sha(b''.join(_bytes(item['lines']) for item in planned)), sum(len(x['lines']) for x in planned)


def _insert_segment(conn, order_id: int, item: dict) -> None:
    """补写一个段；同序号的健康段已存在时跳过，内容不同则拒绝。"""
    existing = conn.execute(select(segments).where(
        segments.c.order_id == order_id, segments.c.ordinal == item['ordinal'])).mappings().first()
    if existing is not None:
        if existing['checksum'] != item['checksum']:
            raise OrderCorrupt(f'顺序单第 {item["ordinal"]} 段已有不同内容')
        return
    try:
        with conn.begin_nested():
            conn.execute(segments.insert().values(
                order_id=order_id,
                ordinal=item['ordinal'],
                start_rank=item['start_rank'],
                end_rank=item['end_rank'],
                lines=item['lines'],
                checksum=item['checksum'],
            ))
    except IntegrityError:
        existing = conn.execute(select(segments).where(
            segments.c.order_id == order_id, segments.c.ordinal == item['ordinal'])).mappings().first()
        if existing is None or existing['checksum'] != item['checksum']:
            raise OrderCorrupt(f'顺序单第 {item["ordinal"]} 段写入冲突') from None


def create_order(conn, key: str, batch_no: str, rows: list[dict], segment_items: int,
                 source: str = 'run', state: str = 'active', evidence_id: int | None = None) -> dict:
    """写入一份顺序单。已有同键同内容的存档时原样返回（幂等）。"""
    planned = _plan(rows, segment_items)
    checksum, total = _whole(planned)
    existing = conn.execute(select(orders).where(orders.c.key == key)).mappings().first()
    if existing is not None:
        if existing['checksum'] != checksum or existing['records'] != total:
            raise OrderCorrupt(f'顺序单 {key} 已有不同内容的存档')
        if state == 'active' and existing['state'] != 'active':
            _activate(conn, existing, checksum, total, len(planned))
        return dict(existing)
    try:
        with conn.begin_nested():
            conn.execute(orders.insert().values(
                key=key, batch_no=batch_no, checksum=checksum, records=total,
                segments=len(planned), format=FORMAT, state='staging', source=source,
                segment_items=max(1, segment_items), evidence_id=evidence_id,
                created_at=utc_time(),
            ))
    except IntegrityError:
        existing = conn.execute(select(orders).where(orders.c.key == key)).mappings().first()
        if existing is None or existing['checksum'] != checksum:
            raise OrderCorrupt(f'顺序单 {key} 存档冲突') from None
        return dict(existing)
    order_id = conn.execute(select(orders.c.id).where(orders.c.key == key)).scalar_one()
    for item in planned:
        _insert_segment(conn, order_id, item)
    # 段全部写完后才发布；同事务内失败时调用方回滚，不会留下半份 active 顺序单。
    if state == 'active':
        conn.execute(orders.update().where(orders.c.id == order_id).values(state='active'))
    return dict(conn.execute(select(orders).where(orders.c.id == order_id)).mappings().first())


def _activate(conn, order: dict, checksum: str, records: int, count: int) -> None:
    conn.execute(orders.update().where(orders.c.id == order['id']).values(
        checksum=checksum, records=records, segments=count, state='active'))


def archive_report(conn, report: object, batch_no: str, payload: object, key: str,
                   source: str = 'run', evidence_id: int | None = None) -> dict:
    rows = _rows_from_report(report)
    return create_order(conn, key, batch_no, rows, segment_items_for(payload),
                        source=source, evidence_id=evidence_id)


def _active(key: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(select(orders).where(
            orders.c.key == key, orders.c.state == 'active')).mappings().first()
        return dict(row) if row else None


def _legacy(key: str) -> tuple[str, str, list[str], int | None]:
    """读取尚未切换的旧证据顺序单，返回批次、校验码、原始行与来源编号。"""
    match = _EVIDENCE_KEY.fullmatch(key)
    if match is None:
        raise HTTPException(404, '找不到该顺序单')
    identity = int(match.group(1))
    with engine.connect() as conn:
        row = conn.execute(
            select(evidence.c.report, cases.c.payload, cases.c.title)
            .join(runs, runs.c.id == evidence.c.run_id)
            .join(cases, cases.c.id == runs.c.case_id)
            .where(evidence.c.id == identity)).mappings().first()
    if row is None:
        raise HTTPException(404, '找不到该顺序单')
    try:
        rows = _rows_from_report(row['report'])
    except OrderCorrupt as exc:
        raise HTTPException(409, f'旧顺序单无法读取：{exc}') from None
    lines = [_line(item) for item in rows]
    payload = row['payload'] if isinstance(row['payload'], dict) else {}
    batch_no = str(payload.get('batch_no') or row['title'])
    return batch_no, _sha(_bytes(lines)), lines, identity


def _describe(order: dict, source: str) -> dict:
    return {
        'key': order['key'], 'batch_no': order['batch_no'], 'checksum': order['checksum'],
        'records': order['records'], 'segments': order['segments'], 'format': order['format'],
        'state': order['state'], 'source': source, 'segment_items': order['segment_items'],
    }


def metadata(key: str) -> dict:
    active = _active(key)
    if active is not None:
        return _describe(active, 'archive')
    batch_no, checksum, lines, identity = _legacy(key)
    return {
        'key': key, 'batch_no': batch_no, 'checksum': checksum,
        'records': len(lines), 'segments': None, 'format': FORMAT,
        'state': 'legacy', 'source': 'legacy', 'legacy_evidence_id': identity,
        'segment_items': None,
    }


def _encode_cursor(key: str, offset: int, size: int, checksum: str) -> str:
    raw = json.dumps({'k': key, 'o': offset, 's': size, 'c': checksum},
                     separators=(',', ':'), sort_keys=True).encode('utf-8')
    signature = hmac.new(AUTH_KEY.encode('utf-8'), raw, hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=') + '.' + signature


def _decode_cursor(token: str) -> dict:
    try:
        body, signature = token.split('.', 1)
        raw = base64.urlsafe_b64decode(body + '=' * (-len(body) % 4))
        expected = hmac.new(AUTH_KEY.encode('utf-8'), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError
        payload = json.loads(raw)
        key, offset, size, checksum = payload['k'], payload['o'], payload['s'], payload['c']
        if not isinstance(key, str) or type(offset) is not int or type(size) is not int:
            raise ValueError
        if offset < 0 or not 1 <= size <= MAX_PAGE_SIZE or not isinstance(checksum, str):
            raise ValueError
        return {'key': key, 'offset': offset, 'size': size, 'checksum': checksum}
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(422, '翻页凭据无效，请从第一页重新读取') from None


def _verify_segment(segment: dict) -> list[str]:
    lines = segment.get('lines')
    expected = segment['end_rank'] - segment['start_rank'] + 1
    if not isinstance(lines, list) or len(lines) != expected:
        raise OrderCorrupt(f'第 {segment["ordinal"]} 段行数与顺位范围不符')
    if not all(isinstance(line, str) for line in lines):
        raise OrderCorrupt(f'第 {segment["ordinal"]} 段包含损坏行')
    if _sha(_bytes(lines)) != segment['checksum']:
        raise OrderCorrupt(f'第 {segment["ordinal"]} 段校验码不符')
    return lines


def _segment_rows(order_id: int, start: int, end: int) -> list[dict]:
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(
            select(segments).where(
                and_(segments.c.order_id == order_id,
                     segments.c.end_rank >= start,
                     segments.c.start_rank <= end))
            .order_by(segments.c.ordinal)).mappings()]


def read_page(key: str, cursor: str | None = None, size: int = 20) -> dict:
    if type(size) is not int or not 1 <= size <= MAX_PAGE_SIZE:
        raise HTTPException(422, f'每页件数必须是 1 至 {MAX_PAGE_SIZE} 之间的整数')
    offset = 0
    expected = None
    if cursor:
        token = _decode_cursor(cursor)
        if token['key'] != key:
            raise HTTPException(422, '翻页凭据不属于该顺序单')
        offset, size, expected = token['offset'], token['size'], token['checksum']

    active = _active(key)
    if active is not None:
        if expected is not None and expected != active['checksum']:
            raise HTTPException(409, '翻页凭据对应的顺序单内容已经变化')
        total = active['records']
        start, end = offset + 1, min(offset + size, total)
        rows: list[dict] = []
        viewed: list[dict] = []
        if start <= end:
            touched = _segment_rows(active['id'], start, end)
            covered = 0
            for segment in touched:
                try:
                    lines = _verify_segment(segment)
                except OrderCorrupt as exc:
                    raise HTTPException(409, f'顺序单存在坏段，无法翻页：{exc}') from None
                low, high = max(start, segment['start_rank']), min(end, segment['end_rank'])
                if low > high:
                    continue
                covered += high - low + 1
                rows.extend(_line_rows(lines[low - segment['start_rank']:high - segment['start_rank'] + 1], low))
            if covered != end - start + 1:
                raise HTTPException(409, '顺序单段范围不完整，无法翻页')
            viewed = [{'ordinal': item['ordinal'], 'start_rank': item['start_rank'],
                       'end_rank': item['end_rank']} for item in touched]
        batch_no, checksum, source = active['batch_no'], active['checksum'], 'archive'
        segment_count, segment_items = active['segments'], active['segment_items']
    else:
        batch_no, checksum, lines, _ = _legacy(key)
        if expected is not None and expected != checksum:
            raise HTTPException(409, '翻页凭据对应的顺序单内容已经变化')
        total = len(lines)
        start, end = offset + 1, min(offset + size, total)
        rows = _line_rows(lines[offset:end], start) if start <= end else []
        viewed = []
        source = 'legacy'
        segment_count = segment_items = None

    return {
        'key': key, 'batch_no': batch_no, 'format': FORMAT, 'checksum': checksum,
        'total': total, 'page_size': size, 'offset': offset,
        'start_rank': start if total else 0, 'end_rank': end,
        'rows': rows, 'segments_read': viewed,
        'segments': segment_count, 'segment_items': segment_items, 'source': source,
        'next_cursor': _encode_cursor(key, end, size, checksum) if end < total else None,
        'prev_cursor': _encode_cursor(key, max(0, offset - size), size, checksum) if offset > 0 else None,
    }


def _assembled(order: dict) -> bytes:
    with engine.connect() as conn:
        stored = [dict(row) for row in conn.execute(
            select(segments).where(segments.c.order_id == order['id'])
            .order_by(segments.c.ordinal)).mappings()]
    if len(stored) != order['segments']:
        raise OrderCorrupt('段数与存档记录不符')
    parts = []
    expected_start = 1
    for index, segment in enumerate(stored, start=1):
        if segment['ordinal'] != index:
            raise OrderCorrupt(f'第 {index} 段序号不连续')
        if segment['start_rank'] != expected_start:
            raise OrderCorrupt(f'第 {index} 段起始顺位不连续')
        expected_start = segment['end_rank'] + 1
        parts.append(_bytes(_verify_segment(segment)))
    if expected_start - 1 != order['records']:
        raise OrderCorrupt('段范围与整单件数不符')
    data = b''.join(parts)
    if _sha(data) != order['checksum']:
        raise OrderCorrupt('整单校验码不符')
    return data


def export_order(key: str) -> tuple[bytes, dict]:
    """按原始格式拼接整单。任一段损坏时拒绝导出。"""
    active = _active(key)
    if active is not None:
        try:
            data = _assembled(active)
        except OrderCorrupt as exc:
            raise HTTPException(409, f'顺序单存在坏段，整单拒绝导出：{exc}') from None
        return data, _describe(active, 'archive')
    batch_no, checksum, lines, identity = _legacy(key)
    data = _bytes(lines)
    if _sha(data) != checksum:
        raise HTTPException(409, '旧顺序单校验不符，整单拒绝导出')
    return data, {
        'key': key, 'batch_no': batch_no, 'checksum': checksum,
        'records': len(lines), 'segments': None, 'format': FORMAT,
        'state': 'legacy', 'source': 'legacy', 'legacy_evidence_id': identity,
        'segment_items': None,
    }


def list_orders() -> list[dict]:
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(
            select(orders).order_by(orders.c.id.desc()).limit(100)).mappings()]


def _pending(conn, limit: int) -> list[dict]:
    rows = conn.execute(
        select(evidence.c.id, evidence.c.report, cases.c.payload, cases.c.title)
        .join(runs, runs.c.id == evidence.c.run_id)
        .join(cases, cases.c.id == runs.c.case_id)
        .outerjoin(orders, orders.c.evidence_id == evidence.c.id)
        .where((orders.c.id.is_(None)) | (orders.c.state != 'active'))
        .order_by(evidence.c.id).limit(limit)).mappings().all()
    return [dict(row) for row in rows]


def migrate_legacy(batch_size: int = 10) -> dict:
    """分批迁移旧证据顺序单。中断后重跑只补缺失段，不会重复写段。"""
    if type(batch_size) is not int or not 1 <= batch_size <= 200:
        raise HTTPException(422, '每批迁移份数必须是 1 至 200 之间的整数')
    with engine.connect() as conn:
        pending = _pending(conn, batch_size)
    results = []
    for item in pending:
        key = evidence_key(item['id'])
        try:
            rows = _rows_from_report(item['report'])
            payload = item['payload'] if isinstance(item['payload'], dict) else {}
            batch_no = str(payload.get('batch_no') or item['title'])
            items = segment_items_for(payload)
            planned = _plan(rows, items)
            checksum, total = _whole(planned)
            with engine.begin() as conn:
                existing = conn.execute(select(orders).where(orders.c.key == key)).mappings().first()
                if existing is not None and existing['state'] == 'active':
                    results.append({'key': key, 'status': 'skipped'})
                    continue
                if existing is None:
                    try:
                        with conn.begin_nested():
                            conn.execute(orders.insert().values(
                                key=key, batch_no=batch_no, checksum=checksum, records=total,
                                segments=len(planned), format=FORMAT, state='staging',
                                source='migration', segment_items=items,
                                evidence_id=item['id'], created_at=utc_time(),
                            ))
                    except IntegrityError:
                        # 另一个迁移进程已建立 staging；继续补写缺失段。
                        pass
                    order_id = conn.execute(
                        select(orders.c.id).where(orders.c.key == key)).scalar_one()
                    present = {row.ordinal for row in conn.execute(
                        select(segments.c.ordinal).where(segments.c.order_id == order_id)).mappings()}
                else:
                    order_id = existing['id']
                    present = {row.ordinal for row in conn.execute(
                        select(segments.c.ordinal).where(segments.c.order_id == order_id)).mappings()}
                for plan in planned:
                    if plan['ordinal'] not in present:
                        _insert_segment(conn, order_id, plan)
            # 独立事务整单校验通过后才原子切换；期间旧证据照常可读可签署。
            with engine.begin() as conn:
                order = conn.execute(select(orders).where(orders.c.key == key)).mappings().first()
                if order is None:
                    raise OrderCorrupt('迁移后找不到顺序单')
                stored = [dict(row) for row in conn.execute(
                    select(segments).where(segments.c.order_id == order['id'])
                    .order_by(segments.c.ordinal)).mappings()]
                if len(stored) != len(planned):
                    raise OrderCorrupt('段数不完整')
                for index, segment in enumerate(stored, start=1):
                    if segment['ordinal'] != index or segment['start_rank'] != planned[index - 1]['start_rank']:
                        raise OrderCorrupt('段范围不连续')
                    _verify_segment(segment)
                if _sha(b''.join(_bytes(segment['lines']) for segment in stored)) != checksum:
                    raise OrderCorrupt('整单校验码不符')
                conn.execute(orders.update().where(orders.c.id == order['id']).values(
                    checksum=checksum, records=total, segments=len(planned), state='active'))
            results.append({'key': key, 'status': 'migrated', 'segments': len(planned)})
        except (OrderCorrupt, ValueError, TypeError, KeyError) as exc:
            results.append({'key': key, 'status': 'failed', 'detail': str(exc)[:200]})
        except Exception:
            logging.exception('Legacy order migration failed for %s', key)
            results.append({'key': key, 'status': 'failed', 'detail': '迁移异常，请查看后端日志'})
    with engine.connect() as conn:
        remaining = len(_pending(conn, 1))
    return {'batch_size': batch_size, 'processed': len(results),
            'results': results, 'more': bool(remaining)}
