import hashlib
import logging
import uuid

from fastapi import HTTPException
from sqlalchemy import and_, or_, select

from src.config.settings import cache, engine
from src.constants.workflow import Status
from src.models.tables import audit, cases, evidence, runs
from src.services import domain, orders
from src.utils.encoding import canonical
from src.utils.time import utc_time


def log(conn, actor, action, reference):
    conn.execute(audit.insert().values(actor=actor, action=action, reference=str(reference), created_at=utc_time()))


def recent(table):
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(select(table).order_by(table.c.id.desc()).limit(100)).mappings()]


def recent_evidence():
    """证据列表只带摘要与指标，上架顺序单独自由 /orders/{key}/page 分段读取。"""
    with engine.connect() as conn:
        rows = conn.execute(select(evidence).order_by(evidence.c.id.desc()).limit(100)).mappings()
        result = []
        for row in rows:
            item = dict(row)
            report = item.get('report') if isinstance(item.get('report'), dict) else {}
            item['report'] = {
                'summary': report.get('summary', ''),
                'metrics': report.get('metrics', {}),
                'order': report.get('order', {}),
                'sections': [section for section in report.get('sections', [])
                             if isinstance(section, dict) and section.get('title') != orders.ORDER_SECTION],
            }
            result.append(item)
        return result


def found(conn, table, identity):
    row = conn.execute(select(table).where(table.c.id == identity)).mappings().first()
    if row is None:
        raise HTTPException(404, '找不到该记录')
    return dict(row)


def make_case(title, payload, actor):
    title = title.strip()
    if not title:
        raise HTTPException(422, '档案名称不能为空')
    try:
        if len(canonical(payload)) > 512 * 1024:
            raise ValueError('业务输入超过 512 KiB')
        if callable(getattr(domain, 'validate', None)):
            domain.validate(payload)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from None
    with engine.begin() as conn:
        case_id = conn.execute(cases.insert().values(slug=uuid.uuid4().hex, title=title, payload=payload,
                               author=actor, created_at=utc_time())).inserted_primary_key[0]
        log(conn, actor, 'create_case', case_id)
        return found(conn, cases, case_id)


def enqueue(case_id, actor):
    with engine.begin() as conn:
        found(conn, cases, case_id)
        run_id = conn.execute(runs.insert().values(case_id=case_id, status=Status.QUEUED,
                              owner=actor, created_at=utc_time())).inserted_primary_key[0]
        log(conn, actor, 'enqueue', run_id)
        result = found(conn, runs, run_id)
    try:
        cache.lpush('worker:wake', 'queued')
        cache.ltrim('worker:wake', 0, 99)
    except Exception:
        # The durable database queue is polled even if Redis is temporarily unavailable.
        pass
    return result


def claim():
    ready = or_(runs.c.status == Status.QUEUED,
                and_(runs.c.status == Status.RUNNING, runs.c.started_at < utc_time(-600)))
    with engine.begin() as conn:
        run = conn.execute(select(runs.c.id, runs.c.case_id).where(ready).order_by(runs.c.id)
                           .limit(1).with_for_update(skip_locked=True)).first()
        if not run:
            return None
        token = str(uuid.uuid4())
        conn.execute(runs.update().where(runs.c.id == run.id).values(
            started_at=utc_time(), status=Status.RUNNING, attempt_token=token))
        return run.id, token, found(conn, cases, run.case_id)['payload']


def finish(run_id, token, result=None, error=None):
    digest = hashlib.sha256(canonical(result)).hexdigest() if result is not None else None
    status = Status.FAILED if error else Status.COMPLETE
    with engine.begin() as conn:
        row = conn.execute(select(runs.c.case_id).where(runs.c.id == run_id)).first()
        count = conn.execute(runs.update().where(runs.c.id == run_id,
                        runs.c.attempt_token == token, runs.c.status == Status.RUNNING)
                        .values(status=status, finished_at=utc_time(), error=error)).rowcount
        if not count:
            return
        if result is not None:
            evidence_id = conn.execute(evidence.insert().values(
                run_id=run_id, report=result, digest=digest)).inserted_primary_key[0]
            payload = conn.execute(select(cases.c.payload).where(
                cases.c.id == row.case_id)).scalar_one() if row else {}
            batch_no = payload.get('batch_no') if isinstance(payload, dict) else None
            try:
                # 存档走独立保存点：写段失败只回滚存档，不影响已完成的执行与证据。
                with conn.begin_nested():
                    orders.archive_report(conn, result, str(batch_no or f'run-{run_id}'), payload,
                                          orders.evidence_key(evidence_id),
                                          source='run', evidence_id=evidence_id)
            except orders.OrderCorrupt:
                # 排序结果本身已校验；存档失败不覆盖执行结论，可由迁移任务补齐。
                pass
            except Exception:
                # 顺序单存档是附加能力，任何存档异常都不应让已完成的执行回滚。
                logging.exception('Order archiving failed for run %s', run_id)
        log(conn, 'worker', status.value, run_id)


def review(identity, note, actor):
    note = note.strip()
    if not note:
        raise HTTPException(422, '复核意见不能为空')
    with engine.begin() as conn:
        found(conn, evidence, identity)
        count = conn.execute(evidence.update().where(evidence.c.id == identity, evidence.c.reviewed_at.is_(None))
                            .values(review_note=note, reviewer=actor, reviewed_at=utc_time())).rowcount
        if count != 1:
            raise HTTPException(409, '该证据已经复核')
        log(conn, actor, 'review', identity)
        return found(conn, evidence, identity)
