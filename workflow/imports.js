import { neon } from '@neondatabase/serverless';

async function updateImport(id, patch) {
  'use step';
  const sql = neon(process.env.DATABASE_URL);
  await sql`UPDATE import_jobs SET status=${patch.status}, processed_rows=${patch.processed_rows}, total_rows=${patch.total_rows}, error_message=${patch.error_message ?? null}, updated_at=now() WHERE id=${id}`;
}

export async function processImport(job) {
  'use workflow';
  await updateImport(job.id, { status: 'PROCESSING', processed_rows: 0, total_rows: 0 });
  // Próximo passo: cada lote baixa o Blob, lê até 1.000 linhas e persiste no Neon.
  await updateImport(job.id, { status: 'QUEUED', processed_rows: 0, total_rows: 0 });
}
