from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.core.clock import to_storage, utc_now

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "samples.db"
_local = threading.local()

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS departments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    manager TEXT NOT NULL,
    phone TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    email TEXT,
    phone TEXT,
    department_id INTEGER REFERENCES departments(id),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled','locked')),
    failed_login_count INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    password_changed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    is_system INTEGER NOT NULL DEFAULT 0 CHECK(is_system IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS permissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    resource TEXT NOT NULL,
    action TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS role_permissions (
    role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    permission_id INTEGER NOT NULL REFERENCES permissions(id) ON DELETE CASCADE,
    granted_at TEXT NOT NULL,
    PRIMARY KEY(role_id, permission_id)
);

CREATE TABLE IF NOT EXISTS user_roles (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    assigned_by INTEGER REFERENCES users(id),
    assigned_at TEXT NOT NULL,
    PRIMARY KEY(user_id, role_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_digest TEXT NOT NULL UNIQUE,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT,
    client_label TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id INTEGER REFERENCES users(id),
    actor_name TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT,
    outcome TEXT NOT NULL CHECK(outcome IN ('success','denied','failure')),
    before_json TEXT,
    after_json TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    correlation_id TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_resource ON audit_events(resource_type, resource_id);

CREATE TABLE IF NOT EXISTS idempotency_records (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS background_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type TEXT NOT NULL,
    deduplication_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','completed','failed','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    locked_at TEXT,
    locked_by TEXT,
    result_json TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_ready ON background_jobs(status, available_at);

CREATE TABLE IF NOT EXISTS storage_locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    building TEXT NOT NULL,
    room TEXT NOT NULL,
    cabinet TEXT NOT NULL,
    shelf TEXT NOT NULL,
    sensitivity TEXT NOT NULL CHECK(sensitivity IN ('normal','restricted','critical')),
    capacity_units INTEGER NOT NULL CHECK(capacity_units > 0),
    version INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collection_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    field_code TEXT NOT NULL UNIQUE,
    project_code TEXT NOT NULL,
    collected_by TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_reference TEXT NOT NULL,
    quantity REAL NOT NULL CHECK(quantity > 0),
    unit TEXT NOT NULL,
    preservation TEXT NOT NULL,
    chain_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS receipt_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_code TEXT NOT NULL UNIQUE,
    project_code TEXT NOT NULL,
    received_by INTEGER NOT NULL REFERENCES users(id),
    received_at TEXT NOT NULL,
    expected_count INTEGER NOT NULL CHECK(expected_count > 0),
    accepted_count INTEGER NOT NULL DEFAULT 0,
    rejected_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK(status IN ('open','reconciled','quarantined','closed')),
    qr_payload TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_code TEXT NOT NULL UNIQUE,
    batch_id INTEGER NOT NULL REFERENCES receipt_batches(id),
    collection_event_id INTEGER REFERENCES collection_events(id),
    parent_sample_id INTEGER REFERENCES samples(id),
    root_sample_id INTEGER REFERENCES samples(id),
    sample_type TEXT NOT NULL,
    quantity REAL NOT NULL CHECK(quantity >= 0),
    reserved_quantity REAL NOT NULL DEFAULT 0 CHECK(reserved_quantity >= 0),
    unit TEXT NOT NULL,
    lifecycle_state TEXT NOT NULL CHECK(lifecycle_state IN ('received','available','loaned','partially_consumed','consumed','quarantined','pending_destruction','destroyed')),
    location_id INTEGER REFERENCES storage_locations(id),
    custody_user_id INTEGER REFERENCES users(id),
    lineage_depth INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(reserved_quantity <= quantity)
);
CREATE INDEX IF NOT EXISTS idx_samples_batch ON samples(batch_id);
CREATE INDEX IF NOT EXISTS idx_samples_parent ON samples(parent_sample_id);
CREATE INDEX IF NOT EXISTS idx_samples_location ON samples(location_id);

CREATE TABLE IF NOT EXISTS aliquot_operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_code TEXT NOT NULL UNIQUE,
    parent_sample_id INTEGER NOT NULL REFERENCES samples(id),
    requested_quantity REAL NOT NULL CHECK(requested_quantity > 0),
    produced_quantity REAL NOT NULL CHECK(produced_quantity >= 0),
    loss_quantity REAL NOT NULL CHECK(loss_quantity >= 0),
    operator_user_id INTEGER NOT NULL REFERENCES users(id),
    occurred_at TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS loans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    loan_code TEXT NOT NULL UNIQUE,
    sample_id INTEGER NOT NULL REFERENCES samples(id),
    borrower_user_id INTEGER NOT NULL REFERENCES users(id),
    approved_request_id INTEGER REFERENCES approval_requests(id),
    quantity REAL NOT NULL CHECK(quantity > 0),
    due_at TEXT NOT NULL,
    returned_quantity REAL NOT NULL DEFAULT 0 CHECK(returned_quantity >= 0),
    state TEXT NOT NULL CHECK(state IN ('active','partially_returned','returned','overdue','disputed')),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(returned_quantity <= quantity)
);

CREATE TABLE IF NOT EXISTS consumption_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id INTEGER NOT NULL REFERENCES samples(id),
    experiment_code TEXT NOT NULL,
    quantity REAL NOT NULL CHECK(quantity > 0),
    operator_user_id INTEGER NOT NULL REFERENCES users(id),
    idempotency_key TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(sample_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS approval_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_code TEXT NOT NULL UNIQUE,
    action_type TEXT NOT NULL CHECK(action_type IN ('loan','destruction','location_reveal','inventory_adjustment')),
    resource_type TEXT NOT NULL,
    resource_id INTEGER NOT NULL,
    requested_by INTEGER NOT NULL REFERENCES users(id),
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','approved','rejected','cancelled','expired','executed')),
    required_approvals INTEGER NOT NULL DEFAULT 2 CHECK(required_approvals >= 2),
    expires_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approval_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL REFERENCES approval_requests(id) ON DELETE CASCADE,
    approver_user_id INTEGER NOT NULL REFERENCES users(id),
    decision TEXT NOT NULL CHECK(decision IN ('approve','reject')),
    comment TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    UNIQUE(request_id, approver_user_id)
);

CREATE TABLE IF NOT EXISTS destruction_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id INTEGER NOT NULL REFERENCES samples(id),
    request_id INTEGER NOT NULL UNIQUE REFERENCES approval_requests(id),
    method TEXT NOT NULL,
    witness_one INTEGER NOT NULL REFERENCES users(id),
    witness_two INTEGER NOT NULL REFERENCES users(id),
    destroyed_quantity REAL NOT NULL CHECK(destroyed_quantity > 0),
    certificate_digest TEXT NOT NULL,
    destroyed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK(witness_one <> witness_two)
);

CREATE TABLE IF NOT EXISTS inventory_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_code TEXT NOT NULL UNIQUE,
    location_id INTEGER NOT NULL REFERENCES storage_locations(id),
    started_by INTEGER NOT NULL REFERENCES users(id),
    state TEXT NOT NULL CHECK(state IN ('draft','counting','reconciling','approved','closed','cancelled')),
    snapshot_version INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    closed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inventory_counts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES inventory_sessions(id) ON DELETE CASCADE,
    sample_id INTEGER NOT NULL REFERENCES samples(id),
    observed_quantity REAL,
    observed_present INTEGER NOT NULL CHECK(observed_present IN (0,1)),
    counted_by INTEGER NOT NULL REFERENCES users(id),
    counted_at TEXT NOT NULL,
    note TEXT NOT NULL,
    UNIQUE(session_id, sample_id)
);

CREATE TABLE IF NOT EXISTS anomaly_cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_code TEXT NOT NULL UNIQUE,
    sample_id INTEGER REFERENCES samples(id),
    batch_id INTEGER REFERENCES receipt_batches(id),
    anomaly_type TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('low','medium','high','critical')),
    state TEXT NOT NULL CHECK(state IN ('open','investigating','contained','resolved','dismissed')),
    detected_by INTEGER NOT NULL REFERENCES users(id),
    description TEXT NOT NULL,
    resolution TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sample_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id INTEGER NOT NULL REFERENCES samples(id),
    event_type TEXT NOT NULL,
    actor_user_id INTEGER REFERENCES users(id),
    quantity_delta REAL NOT NULL DEFAULT 0,
    from_state TEXT,
    to_state TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    correlation_id TEXT,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sample_events_sample ON sample_events(sample_id, id);
"""

PERMISSIONS = [
    ("users.read", "查看用户", "users", "read"),
    ("users.write", "维护用户", "users", "write"),
    ("roles.read", "查看角色", "roles", "read"),
    ("roles.write", "维护角色", "roles", "write"),
    ("audit.read", "查看审计", "audit", "read"),
    ("jobs.run", "执行后台任务", "jobs", "run"),
    ("samples.read", "查看样品", "samples", "read"),
    ("samples.write", "维护样品", "samples", "write"),
    ("samples.consume", "登记消耗", "samples", "consume"),
    ("samples.destroy", "执行销毁", "samples", "destroy"),
    ("loans.manage", "管理借用", "loans", "manage"),
    ("inventory.manage", "管理盘点", "inventory", "manage"),
    ("approvals.decide", "审批高风险操作", "approvals", "decide"),
    ("locations.read_sensitive", "查看精确保管位置", "locations", "read_sensitive"),
    ("anomalies.manage", "管理异常", "anomalies", "manage"),
]


def database_path() -> Path:
    raw = os.getenv("SAMPLE_DATABASE_PATH", str(DEFAULT_DB_PATH))
    return Path(raw).expanduser().resolve()


def _create_connection() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, check_same_thread=False, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def get_connection() -> sqlite3.Connection:
    connection = getattr(_local, "connection", None)
    if connection is None:
        connection = _create_connection()
        _local.connection = connection
    return connection


def close_connection() -> None:
    connection = getattr(_local, "connection", None)
    if connection is not None:
        connection.close()
        _local.connection = None


@contextmanager
def transaction(*, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    connection = get_connection()
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


def init_db() -> None:
    now = to_storage(utc_now())
    connection = get_connection()
    connection.executescript(SCHEMA)
    with transaction(immediate=True) as connection:
        for code, name, resource, action in PERMISSIONS:
            connection.execute(
                "INSERT OR IGNORE INTO permissions(code,name,resource,action) VALUES(?,?,?,?)",
                (code, name, resource, action),
            )
        roles = (
            ("administrator", "系统管理员", "拥有全部系统权限"),
            ("sample_manager", "样品管理员", "维护样品、批次、位置、借用与盘点"),
            ("researcher", "研究人员", "查看样品并申请借用或登记实验消耗"),
            ("approver", "风险审批人", "复核销毁、位置解密与盘点调整"),
            ("auditor", "审计查看员", "只读查看样品事件和审计记录"),
        )
        for code, name, description in roles:
            connection.execute(
                "INSERT OR IGNORE INTO roles(code,name,description,is_system,created_at,updated_at) VALUES(?,?,?,1,?,?)",
                (code, name, description, now, now),
            )
        administrator = connection.execute("SELECT id FROM roles WHERE code='administrator'").fetchone()[0]
        connection.execute(
            "INSERT OR IGNORE INTO role_permissions(role_id,permission_id,granted_at) SELECT ?,id,? FROM permissions",
            (administrator, now),
        )
        role_permissions = {
            "sample_manager": [
                "samples.read", "samples.write", "samples.consume", "samples.destroy",
                "loans.manage", "inventory.manage", "anomalies.manage",
            ],
            "researcher": ["samples.read", "samples.consume"],
            "approver": ["samples.read", "approvals.decide"],
            "auditor": ["samples.read", "audit.read"],
        }
        for role_code, permission_codes in role_permissions.items():
            role_id = connection.execute("SELECT id FROM roles WHERE code=?", (role_code,)).fetchone()[0]
            placeholders = ",".join("?" for _ in permission_codes)
            connection.execute(
                f"INSERT OR IGNORE INTO role_permissions(role_id,permission_id,granted_at) "
                f"SELECT ?,id,? FROM permissions WHERE code IN ({placeholders})",
                (role_id, now, *permission_codes),
            )


def migrate_db() -> None:
    init_db()
