-- Recuperação não destrutiva para bancos próximos da cota de armazenamento.
-- O índice único (run_id, source, row_number) já atende buscas pelo prefixo
-- (run_id, source), portanto este índice adicional é redundante.
DROP INDEX IF EXISTS source_records_run_source_idx;

-- As consultas atuais de actual_movements filtram pela execução inteira ou
-- acessam IDs primários. O índice abaixo tinha uso residual e pode ser recriado
-- futuramente se uma consulta por NF/série voltar a existir.
DROP INDEX IF EXISTS actual_movements_document_idx;

ANALYZE source_records;
