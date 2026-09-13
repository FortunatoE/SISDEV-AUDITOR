-- Fila operacional persistente. Seguro para executar mais de uma vez no Neon.
CREATE TABLE IF NOT EXISTS work_items (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL,
    document_key TEXT NOT NULL,
    center TEXT,
    nf TEXT,
    series TEXT,
    direction TEXT,
    assigned_to BIGINT REFERENCES app_users(id),
    status TEXT NOT NULL DEFAULT 'NOVA',
    priority TEXT NOT NULL DEFAULT 'MEDIA',
    due_date TEXT,
    created_by BIGINT REFERENCES app_users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    UNIQUE(run_id, document_key)
);

CREATE TABLE IF NOT EXISTS work_item_comments (
    id BIGSERIAL PRIMARY KEY,
    work_item_id BIGINT NOT NULL REFERENCES work_items(id),
    author_user_id BIGINT NOT NULL REFERENCES app_users(id),
    comment TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS work_items_queue_idx
ON work_items(run_id, status, priority, due_date);

CREATE INDEX IF NOT EXISTS work_items_center_idx
ON work_items(center, assigned_to, updated_at);

CREATE INDEX IF NOT EXISTS work_item_comments_item_idx
ON work_item_comments(work_item_id, created_at);
