# SISDEV AUDITOR

Aplicação web para auditoria, conciliação e preparação operacional de lançamentos entre SAP, SISDEV e Agrotis. O sistema identifica documentos pendentes ou divergentes, prioriza o lote do fabricante e prepara a fila “Regularizar SISDEV” para conferência humana.

> O SISDEV AUDITOR não realiza lançamentos automáticos no SISDEV.

## Fluxo operacional

1. O usuário envia cada fonte separadamente na guia **Importar arquivos**.
2. O arquivo original é armazenado no Vercel Blob.
3. A API registra um `import_job` no Neon e devolve o `job_id`.
4. O botão **Processar esta fonte** inicia um Vercel Workflow assíncrono.
5. A fonte é lida uma única vez e gravada no Neon em lotes de 1.000 registros, com progresso persistido.
6. A interface consulta o job e mostra `Aguardando`, `Processando`, `Concluído`, `Concluído com alertas` ou `Falhou`.
7. Depois das oito fontes obrigatórias, **Conciliar fontes concluídas** executa a conciliação em uma etapa separada.
8. Dashboard, páginas e exportações leem somente a execução consolidada no Neon.

O clique HTTP apenas inicia o Workflow. O processamento não fica preso ao tempo da requisição do navegador.

## Fontes de dados

| Etapa | Fonte | Formato |
| --- | --- | --- |
| 1 | SAP — Entradas atuais | `.xlsx` ou `.xls` |
| 2 | SAP — Saídas atuais | `.xlsx` ou `.xls` |
| 3 | SAP — Entrada histórica | `.xlsx` ou `.xls` |
| 4 | SAP — Saída histórica | `.xlsx` ou `.xls` |
| 5 | SAP — Estoque (MB52) | `.xlsx` ou `.xls` |
| 6 | SISDEV — Estoque / Relatório Saldo de Agrotóxico | `.pdf` textual |
| 7 | SISDEV — Movimentações | `.xlsx` ou `.xls` |
| 8 | Agrotis — Receitas | `.xlsx` ou `.xls` |

O upload multipart atual aceita até **4 MB por arquivo**, margem segura para o limite da Function e suficiente para as fontes operacionais atuais. Arquivos maiores deverão usar upload direto do navegador para o Blob.

## Regras implementadas

- SAP é a referência operacional da quantidade.
- Entrada e saída são derivadas da fonte SAP quando a planilha não possui direção explícita.
- NF e série são normalizadas antes da comparação.
- A conciliação considera NF, série, produto, direção, data, centro quando disponível, lote e quantidade.
- Um movimento SISDEV não pode ser reutilizado em duas linhas SAP.
- O lote do fabricante é prioritário; divergência desse lote é classificada como divergência, não como correto.
- Quantidade SISDEV é calculada pelo valor absoluto de `embalagens × volume`.
- Linhas exatamente duplicadas da exportação de movimentos SISDEV são removidas antes da conciliação e registradas como alerta.
- Datas brasileiras são interpretadas com dia antes do mês.
- Estoque é comparado por centro, produto, lote do fabricante e unidade.
- Diferença de estoque é `SISDEV - SAP`: positiva necessita saída; negativa necessita entrada; zero está equilibrado.
- Receita de saída é procurada para o mesmo produto em **D ou D-1** da data do documento.
- A preferência de responsável técnico é configurável; o padrão é **KARLA DANIELLY GARCIA DE LIMA**.
- Doses `g/ha` são convertidas para `kg/ha` e `mL/ha` para `L/ha` antes de calcular a área.

Exemplo: `40 L ÷ 0,06 L/ha = 666,67 ha`.

## Funcionalidades

- Dashboard com documentos distintos, eficácia, aderência e distribuição real dos status.
- Filtros por período, centro, direção e preferência de RT.
- Pendências com análise linha a linha e comparação de lotes/quantidades.
- Fila operacional **Regularizar SISDEV** para entradas e saídas.
- Notas fiscais, receitas, movimentações e estoques em visões próprias.
- Cadastros/dimensões derivados dos dados importados.
- Mapeamentos persistentes de produto, propriedade/CNPJ, CNPJ-centro/URE, lote fabricante e regras.
- Exportação CSV e XLSX de relatórios e da fila operacional.
- Histórico de importações, eventos, progresso, alertas e causa amigável de falha.

### Regularizar SISDEV — saída

A exportação inclui data, CNPJ, NF-e, série, produto, lote, quantidade, volume e quantidade de embalagem, receituário, ART, RT, cultura, diagnóstico, URE, dose e área calculada.

## Arquitetura

```text
Navegador
   ├── upload multipart ───────────────> Vercel Blob (arquivo original)
   ├── iniciar/consultar job ──────────> Flask API
   └── consultar dashboard/exportar ──> Flask API
                                          │
                                          ├── Vercel Workflow (processamento e retry)
                                          └── Neon Postgres (jobs, dados e resultados)
```

```text
api/index.py                  API Flask e entrada Vercel
workflow/imports.py           Workflows duráveis de importação e conciliação
src/auditor/database.py       SQLite local / Neon e schema
src/auditor/engine.py         parsers, importação em lote e regras de conciliação
src/auditor/normalization.py  números, documentos, lotes, unidades e texto
src/web/                      interface web estática
sql/import_jobs.sql           migração idempotente do Neon
tests/                        testes de motor, API e orquestração
pyproject.toml                dependências e registro do Workflow Python
vercel.json                   roteamento e limites da Function
```

### Persistência

- `import_runs`: execução consolidada consumida pelo dashboard.
- `import_batches`: ciclo que reúne as oito fontes.
- `import_jobs`: estado, cursor, contadores, tentativas e ID do Workflow por fonte.
- `import_job_events`: histórico do progresso e das falhas.
- `source_records`: linhas normalizadas das fontes enviadas.
- `expected_movements` / `actual_movements`: movimentos SAP e SISDEV.
- `reconciliations` / `audit_issues`: resultado e alertas.
- `reconciliation_mappings` / `app_settings`: premissas e mapeamentos configuráveis.

Em produção, `Acompanhamento SISDEV.xlsx` e o PBIX **não são fontes de dados**. A estrutura histórica serviu como referência para regras e colunas; os registros usados são exclusivamente os arquivos enviados e persistidos no Neon.

## API principal

| Método e rota | Função |
| --- | --- |
| `POST /api/upload` | Envia uma fonte ao Blob e cria o job |
| `POST /api/import/{job_id}` | Inicia o Workflow da fonte |
| `GET /api/import/{job_id}` | Consulta progresso e eventos |
| `POST /api/import/{job_id}/retry` | Retoma uma fonte com falha |
| `GET /api/import-jobs/latest` | Restaura o ciclo atual na interface |
| `POST /api/reconcile` | Inicia a conciliação das fontes concluídas |
| `GET /api/reconcile/{batch_id}` | Consulta a conciliação |
| `GET /api/dashboard` | Indicadores, gráficos, filtros e alertas |
| `GET /api/page/{pagina}` | Dados de uma guia |
| `GET /api/export/{csv|xlsx}/{pagina}` | Exportação operacional |
| `GET/POST /api/settings/rt-preference` | Preferência de RT |
| `GET/POST /api/mappings` | Premissas e mapeamentos |
| `GET /api/health` | Saúde básica da API |

## Configuração

Variáveis obrigatórias na Vercel:

```text
DATABASE_URL=<conexão pooled do Neon>
BLOB_READ_WRITE_TOKEN=<token do Vercel Blob>
```

Mantenha `DATABASE_URL_UNPOOLED` somente para migrações administrativas quando disponibilizada pela integração Neon.

### Banco

Antes da primeira publicação da fila, execute no Neon:

```text
sql/import_jobs.sql
```

O script é idempotente e pode ser reaplicado em atualizações de schema.

## Desenvolvimento e testes

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest -q
vercel dev
```

Abra a URL informada por `vercel dev`. Esse comando é o modo local recomendado para testar API e Workflow juntos.

Validações antes de publicar:

```powershell
node --check src\web\app.js
vercel build
vercel deploy
```

Promova para produção somente depois de validar o preview:

```powershell
vercel deploy --prod
```

## Segurança dos dados

Planilhas, PDFs, banco local, arquivos temporários, variáveis de ambiente e artefatos operacionais estão excluídos pelo `.gitignore`. Como as fontes podem conter CNPJ, propriedade e dados operacionais, uma instalação pública deve adicionar autenticação e autorização antes de uso por múltiplos usuários.
