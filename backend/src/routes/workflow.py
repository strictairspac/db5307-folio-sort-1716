from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from src.config.settings import cache, engine
from src.controllers.workflow import download, download_order
from src.models.tables import audit, cases, runs
from src.schemas.workflow import CaseForm, LoginForm, ReviewForm
from src.services import domain, orders, workflow
from src.services.security import current_user, login, operator, reviewer

router = APIRouter()


@router.get('/health')
def health():
    with engine.connect() as conn:
        conn.execute(text('SELECT 1')).scalar_one()
    cache.ping()
    return {'ok': True}


@router.post('/login')
def sign_in(form: LoginForm):
    return login(form.username, form.password)


@router.get('/spec')
def spec(_=Depends(current_user)):
    return domain.SPEC


@router.get('/cases')
def list_cases(_=Depends(current_user)):
    return workflow.recent(cases)


@router.post('/cases', status_code=201)
def create_case(form: CaseForm, actor=Depends(operator)):
    return workflow.make_case(form.title, form.payload, actor)


@router.get('/runs')
def list_runs(_=Depends(current_user)):
    return workflow.recent(runs)


@router.post('/cases/{case_id}/runs', status_code=202)
def start_run(case_id: int, actor=Depends(operator)):
    return workflow.enqueue(case_id, actor)


@router.get('/evidence')
def list_evidence(_=Depends(current_user)):
    return workflow.recent_evidence()


@router.get('/evidence/{identity}/download')
def export_evidence(identity: int, _=Depends(current_user)):
    return download(identity)


@router.get('/orders')
def list_orders(_=Depends(current_user)):
    return orders.list_orders()


@router.post('/orders/migrate')
def migrate_orders(batch_size: int = Query(default=10, ge=1, le=200), _=Depends(operator)):
    return orders.migrate_legacy(batch_size)


# 键为 ``evidence:<id>``：保持在路径转换器中，避免 ``/`` 分隔的固定后缀被整体吞掉。
@router.get('/orders/{key}/page')
def order_page(key: str, cursor: str | None = Query(default=None, max_length=4096),
               size: int = Query(default=20, ge=1, le=200), _=Depends(current_user)):
    return orders.read_page(key, cursor, size)


@router.get('/orders/{key}/export')
def export_order(key: str, _=Depends(current_user)):
    return download_order(key)


@router.get('/orders/{key}')
def order_metadata(key: str, _=Depends(current_user)):
    return orders.metadata(key)


@router.post('/evidence/{identity}/review')
def review_evidence(identity: int, form: ReviewForm, actor=Depends(reviewer)):
    return workflow.review(identity, form.note, actor)


@router.get('/audit')
def list_audit(_=Depends(current_user)):
    return workflow.recent(audit)
