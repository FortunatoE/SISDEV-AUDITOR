"""Persistent operational state layered on top of reconciliation results."""

from __future__ import annotations

from datetime import date
from typing import Any

from .database import connect
from .engine import (
    _latest_success,
    _regularization_queue_rows,
    preferred_rt,
    regularization_document_detail,
)
from .normalization import key, text


WORK_STATUSES = (
    "NOVA", "EM_ANALISE", "AGUARDANDO_INFORMACAO", "REGULARIZADA", "VALIDADA",
)
WORK_PRIORITIES = ("BAIXA", "MEDIA", "ALTA")


def _page_values(filters: dict[str, Any]) -> tuple[int, int]:
    try:
        page = max(1, int(filters.get("page") or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = int(filters.get("per_page") or 50)
    except (TypeError, ValueError):
        per_page = 50
    return page, per_page if per_page in {25, 50, 100, 250} else 50


def work_queue_rows(filters: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return current pending documents enriched with persistent work state."""

    filters = dict(filters or {})
    conn = connect()
    try:
        run = _latest_success(conn)
        if not run:
            return {"ready": False, "rows": [], "pagination": {"page": 1, "per_page": 50, "total": 0, "total_pages": 0, "from": 0, "to": 0}}
        run_id = int(run["id"])
        operational_filters = dict(filters)
        operational_filters["status"] = ""
        rows = _regularization_queue_rows(
            conn, run_id, filters.get("preferred_rt") or preferred_rt(conn), operational_filters,
        )
        persisted = {
            row["document_key"]: dict(row)
            for row in conn.execute(
                """SELECT w.*,u.display_name assigned_name,
                          (SELECT COUNT(*) FROM work_item_comments c WHERE c.work_item_id=w.id) comments_count
                   FROM work_items w LEFT JOIN app_users u ON u.id=w.assigned_to
                   WHERE w.run_id=?""",
                (run_id,),
            )
        }
        for row in rows:
            saved = persisted.get(row["document_id"], {})
            work_status = saved.get("status") or "NOVA"
            due_date = saved.get("due_date")
            today = date.today().isoformat()
            if key(work_status) in {"REGULARIZADA", "VALIDADA"}:
                due_status = "CONCLUIDA"
            elif not due_date:
                due_status = "SEM_PRAZO"
            elif str(due_date) < today:
                due_status = "ATRASADA"
            elif str(due_date) == today:
                due_status = "VENCE_HOJE"
            else:
                due_status = "NO_PRAZO"
            row.update({
                "work_item_id": saved.get("id"),
                "andamento": work_status,
                "responsavel_id": saved.get("assigned_to"),
                "responsavel": saved.get("assigned_name") or "Não atribuído",
                "prioridade_trabalho": saved.get("priority") or key(row.get("prioridade") or "MEDIA"),
                "prazo": due_date,
                "situacao_prazo": due_status,
                "comentarios": int(saved.get("comments_count") or 0),
                "atualizado_em": saved.get("updated_at"),
            })

        requested_status = key(filters.get("status"))
        if requested_status:
            rows = [row for row in rows if key(row.get("andamento")) == requested_status]
        requested_priority = key(filters.get("priority"))
        if requested_priority:
            rows = [row for row in rows if key(row.get("prioridade_trabalho")) == requested_priority]
        assigned = text(filters.get("assigned_to"))
        if assigned:
            if assigned == "__unassigned__":
                rows = [row for row in rows if not row.get("responsavel_id")]
            else:
                rows = [row for row in rows if str(row.get("responsavel_id") or "") == assigned]
        requested_due = key(filters.get("due_status"))
        if requested_due:
            rows = [row for row in rows if key(row.get("situacao_prazo")) == requested_due]

        priority_order = {"ALTA": 0, "MEDIA": 1, "BAIXA": 2}
        status_order = {value: index for index, value in enumerate(WORK_STATUSES)}
        sort_name = key(filters.get("sort") or "PRIORIDADE")
        sorters = {
            "PRAZO": lambda row: (row.get("prazo") or "9999-12-31",),
            "DATA": lambda row: (row.get("data_documento") or "",),
            "NF": lambda row: (row.get("numero_nfe") or "",),
            "STATUS": lambda row: (status_order.get(key(row.get("andamento")), 99),),
            "RESPONSAVEL": lambda row: (key(row.get("responsavel")),),
            "CENTRO": lambda row: (row.get("centro") or "",),
            "PRIORIDADE": lambda row: (priority_order.get(key(row.get("prioridade_trabalho")), 99),),
            "PRIORIDADE_TRABALHO": lambda row: (priority_order.get(key(row.get("prioridade_trabalho")), 99),),
            "NUMERO_NFE": lambda row: (row.get("numero_nfe") or "",),
            "DATA_DOCUMENTO": lambda row: (row.get("data_documento") or "",),
            "ANDAMENTO": lambda row: (status_order.get(key(row.get("andamento")), 99),),
            "SITUACAO_PRAZO": lambda row: (key(row.get("situacao_prazo")),),
        }
        sorter = sorters.get(sort_name, sorters["PRIORIDADE"])
        reverse = text(filters.get("order")).lower() == "desc"
        rows.sort(key=lambda row: (*sorter(row), row.get("data_documento") or "", row.get("document_id") or ""), reverse=reverse)
        page, per_page = _page_values(filters)
        total = len(rows)
        total_pages = (total + per_page - 1) // per_page if total else 0
        if total_pages and page > total_pages:
            page = total_pages
        offset = (page - 1) * per_page
        visible = rows if filters.get("export") else rows[offset:offset + per_page]
        return {
            "ready": True,
            "run_id": run_id,
            "rows": visible,
            "summary": {
                "total": total,
                "novas": sum(key(row.get("andamento")) == "NOVA" for row in rows),
                "em_analise": sum(key(row.get("andamento")) == "EM_ANALISE" for row in rows),
                "aguardando": sum(key(row.get("andamento")) == "AGUARDANDO_INFORMACAO" for row in rows),
                "vencidas": sum(bool(row.get("prazo")) and str(row["prazo"]) < date.today().isoformat() and key(row.get("andamento")) not in {"REGULARIZADA", "VALIDADA"} for row in rows),
                "vence_hoje": sum(key(row.get("situacao_prazo")) == "VENCE_HOJE" for row in rows),
                "sem_responsavel": sum(not row.get("responsavel_id") for row in rows),
                "concluidas": sum(key(row.get("andamento")) in {"REGULARIZADA", "VALIDADA"} for row in rows),
            },
            "pagination": {
                "page": page, "per_page": per_page, "total": total,
                "total_pages": total_pages, "from": offset + 1 if visible else 0,
                "to": offset + len(visible),
            },
        }
    finally:
        conn.close()


def work_queue_export_rows(filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    export_filters = dict(filters or {})
    export_filters["export"] = "1"
    payload = work_queue_rows(export_filters)
    columns = (
        ("Prioridade", "prioridade_trabalho"), ("Andamento", "andamento"),
        ("Situação do prazo", "situacao_prazo"), ("Prazo", "prazo"),
        ("Responsável", "responsavel"), ("Número da NF-e", "numero_nfe"),
        ("Série", "serie"), ("Data do documento", "data_documento"),
        ("Centro", "centro"), ("Direção", "direcao"), ("Situação", "situacao"),
        ("Diagnóstico", "diagnostico_situacao"), ("Ação recomendada", "acao_recomendada"),
        ("Produto", "produto"), ("Comentários", "comentarios"),
    )
    return [
        {label: row.get(field) for label, field in columns}
        for row in payload.get("rows", [])
    ]


def active_assignees() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(
            """SELECT id,display_name,profile FROM app_users
               WHERE active=1 AND deleted_at IS NULL ORDER BY display_name"""
        )]
    finally:
        conn.close()


def save_work_item(
    document_key: str,
    filters: dict[str, Any],
    values: dict[str, Any],
    actor_user_id: int | None,
    *,
    can_manage: bool,
) -> dict[str, Any] | None:
    detail = regularization_document_detail(document_key, filters)
    if not detail:
        return None
    document = detail["document"]
    status = key(values.get("status") or "NOVA")
    if status not in WORK_STATUSES:
        raise ValueError("Andamento inválido.")
    if status == "VALIDADA" and not can_manage:
        raise ValueError("Somente gestores podem validar uma tarefa.")
    priority = key(values.get("priority") or document.get("prioridade") or "MEDIA")
    if priority not in WORK_PRIORITIES:
        raise ValueError("Prioridade inválida.")
    assigned_to = values.get("assigned_to")
    assigned_to = int(assigned_to) if str(assigned_to or "").isdigit() else None
    due_date = text(values.get("due_date")) or None
    if due_date:
        try:
            date.fromisoformat(due_date)
        except ValueError as error:
            raise ValueError("Prazo inválido.") from error

    conn = connect()
    try:
        current = conn.execute(
            "SELECT * FROM work_items WHERE run_id=? AND document_key=?",
            (detail["run_id"], document_key),
        ).fetchone()
        if not can_manage and current:
            # Operadores e auditores podem movimentar o trabalho, mas não
            # redistribuir responsabilidade, prazo ou prioridade.
            assigned_to = current.get("assigned_to")
            due_date = current.get("due_date")
            priority = current.get("priority") or priority
        elif not can_manage:
            assigned_to = actor_user_id
        if assigned_to is not None:
            user = conn.execute(
                "SELECT id,profile FROM app_users WHERE id=? AND active=1 AND deleted_at IS NULL",
                (assigned_to,),
            ).fetchone()
            if not user:
                raise ValueError("Responsável inválido ou inativo.")
            center = text(document.get("centro"))
            if key(user["profile"]) != "ADMINISTRADOR" and center:
                allowed = conn.execute(
                    """SELECT 1 FROM user_scopes
                       WHERE user_id=? AND scope_type IN ('CENTER','UNIT') AND scope_value=? LIMIT 1""",
                    (assigned_to, center),
                ).fetchone()
                if not allowed:
                    raise ValueError("O responsável não possui acesso ao centro desta tarefa.")
        completed = "CURRENT_TIMESTAMP" if status in {"REGULARIZADA", "VALIDADA"} else "NULL"
        row = conn.execute(
            f"""INSERT INTO work_items(
                    run_id,document_key,center,nf,series,direction,assigned_to,status,
                    priority,due_date,created_by,completed_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,{completed})
                ON CONFLICT(run_id,document_key) DO UPDATE SET
                    center=excluded.center,nf=excluded.nf,series=excluded.series,
                    direction=excluded.direction,assigned_to=excluded.assigned_to,
                    status=excluded.status,priority=excluded.priority,due_date=excluded.due_date,
                    completed_at={completed},updated_at=CURRENT_TIMESTAMP
                RETURNING *""",
            (
                detail["run_id"], document_key, document.get("centro"),
                document.get("numero_nf"), document.get("serie"), document.get("direcao"),
                assigned_to, status, priority, due_date, actor_user_id,
            ),
        ).fetchone()
        conn.commit()
        return dict(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def add_work_comment(
    document_key: str,
    filters: dict[str, Any],
    comment: str,
    actor_user_id: int,
) -> dict[str, Any] | None:
    detail = regularization_document_detail(document_key, filters)
    if not detail:
        return None
    document = detail["document"]
    conn = connect()
    try:
        item = conn.execute(
            "SELECT * FROM work_items WHERE run_id=? AND document_key=?",
            (detail["run_id"], document_key),
        ).fetchone()
        if not item:
            item = conn.execute(
                """INSERT INTO work_items(
                       run_id,document_key,center,nf,series,direction,assigned_to,status,
                       priority,created_by
                   ) VALUES (?,?,?,?,?,?,?,?,?,?) RETURNING *""",
                (
                    detail["run_id"], document_key, document.get("centro"),
                    document.get("numero_nf"), document.get("serie"), document.get("direcao"),
                    actor_user_id, "EM_ANALISE", key(document.get("prioridade") or "MEDIA"),
                    actor_user_id,
                ),
            ).fetchone()
        row = conn.execute(
            """INSERT INTO work_item_comments(work_item_id,author_user_id,comment)
               VALUES (?,?,?) RETURNING *""",
            (item["id"], actor_user_id, comment),
        ).fetchone()
        conn.execute(
            "UPDATE work_items SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (item["id"],)
        )
        conn.commit()
        return dict(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def work_comments(document_key: str, filters: dict[str, Any]) -> list[dict[str, Any]] | None:
    detail = regularization_document_detail(document_key, filters)
    if not detail:
        return None
    conn = connect()
    try:
        item = conn.execute(
            "SELECT id FROM work_items WHERE run_id=? AND document_key=?",
            (detail["run_id"], document_key),
        ).fetchone()
        if not item:
            return []
        return [dict(row) for row in conn.execute(
            """SELECT c.id,c.comment,c.created_at,u.display_name author
               FROM work_item_comments c JOIN app_users u ON u.id=c.author_user_id
               WHERE c.work_item_id=? ORDER BY c.created_at DESC""",
            (item["id"],),
        )]
    finally:
        conn.close()
