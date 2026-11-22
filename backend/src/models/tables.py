from sqlalchemy import (JSON, Column, ForeignKey, Index, Integer, MetaData,
                        String, Table, Text, UniqueConstraint)

schema = MetaData()
users = Table('accounts', schema,
    Column('id', Integer, primary_key=True),
    Column('username', String(40), nullable=False, unique=True),
    Column('password_hash', String(200), nullable=False),
    Column('role', String(20), nullable=False))
cases = Table('business_cases', schema,
    Column('id', Integer, primary_key=True),
    Column('slug', String(64), nullable=False, unique=True),
    Column('title', String(120), nullable=False),
    Column('payload', JSON, nullable=False),
    Column('author', String(40), nullable=False),
    Column('created_at', String(32), nullable=False))
runs = Table('business_runs', schema,
    Column('id', Integer, primary_key=True),
    Column('case_id', ForeignKey('business_cases.id'), nullable=False, index=True),
    Column('status', String(20), nullable=False, index=True),
    Column('owner', String(40), nullable=False),
    Column('attempt_token', String(36)),
    Column('started_at', String(32)),
    Column('finished_at', String(32)),
    Column('created_at', String(32), nullable=False),
    Column('error', Text))
evidence = Table('business_evidence', schema,
    Column('id', Integer, primary_key=True),
    Column('run_id', ForeignKey('business_runs.id'), nullable=False, unique=True),
    Column('report', JSON, nullable=False),
    Column('digest', String(64), nullable=False),
    Column('reviewer', String(40)),
    Column('review_note', String(500)),
    Column('reviewed_at', String(32)))
audit = Table('audit_events', schema,
    Column('id', Integer, primary_key=True),
    Column('actor', String(40), nullable=False),
    Column('action', String(32), nullable=False),
    Column('reference', String(50), nullable=False),
    Column('created_at', String(32), nullable=False))
# 顺序单整份存档：一份顺序单由若干连续段组成，段内按顺位保存原始行文本。
orders = Table('folio_orders', schema,
    Column('id', Integer, primary_key=True),
    Column('key', String(64), nullable=False, unique=True),
    Column('batch_no', String(120), nullable=False),
    Column('checksum', String(64), nullable=False),
    Column('records', Integer, nullable=False),
    Column('segments', Integer, nullable=False),
    Column('format', String(32), nullable=False),
    Column('state', String(16), nullable=False, index=True),
    Column('source', String(16), nullable=False),
    Column('segment_items', Integer, nullable=False),
    Column('evidence_id', Integer, ForeignKey('business_evidence.id'), unique=True),
    Column('created_at', String(32), nullable=False))
segments = Table('folio_segments', schema,
    Column('id', Integer, primary_key=True),
    Column('order_id', ForeignKey('folio_orders.id'), nullable=False, index=True),
    Column('ordinal', Integer, nullable=False),
    Column('start_rank', Integer, nullable=False),
    Column('end_rank', Integer, nullable=False),
    Column('lines', JSON, nullable=False),
    Column('checksum', String(64), nullable=False),
    UniqueConstraint('order_id', 'ordinal', name='uq_folio_segment_ordinal'))
Index('ix_folio_segment_start', segments.c.order_id, segments.c.start_rank)
