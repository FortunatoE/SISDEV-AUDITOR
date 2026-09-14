-- Recuperação não destrutiva para bancos próximos da cota de armazenamento.
-- O índice único (run_id, source, row_number) já atende buscas pelo prefixo
-- (run_id, source), portanto este índice adicional é redundante.
DROP INDEX IF EXISTS source_records_run_source_idx;

ANALYZE source_records;
