# SISDEV AUDITOR

Aplicação web para auditoria, conciliação e preparação operacional de lançamentos entre SAP, SISDEV e Agrotis. O sistema identifica documentos pendentes ou divergentes, prioriza o lote do fabricante e prepara a fila “Regularizar SISDEV” para conferência humana.

> O SISDEV AUDITOR não realiza lançamentos automáticos no SISDEV.

## Versão  — 27 agosto de 2026

Esta versão consolida a evolução da aplicação para uma plataforma protegida de análise, auditoria e orientação operacional. Está publicada em [sisdev-auditor.vercel.app](https://sisdev-auditor.vercel.app/).

Principais melhorias entregues:

- login seguro com sessão revogável, logout, proteção CSRF e limitação de tentativas;
- perfis `ADMINISTRADOR`, `GESTOR`, `AUDITOR`, `OPERADOR` e `CONSULTA`;
- autorização validada no backend por perfil, permissão e escopo;
- criação e administração de usuários restritas ao painel do administrador;
- escopos de acesso por centro, unidade e propriedade;
- paginação de tabelas com 25, 50, 100 e 250 registros por página;
- filtros combinados por período, centro, operação, status e NF-e;
- exportação filtrada em CSV e XLSX com proteção contra CSV Injection;
- Pendências com situação estruturada, diagnóstico, ação recomendada e confiança;
- Regularizar SISDEV separado entre entrada e saída, com dados operacionais completos;
- cálculo inteligente do saldo: `OK`, `SALDO_PARCIAL` e `SEM_SALDO`;
- trilha de auditoria para autenticação, importação, exportação, cadastros, mapeamentos e tratamento de pendências;
- uploads validados e arquivos originais privados no Vercel Blob;
- backup privado de configurações com verificação e restauração administrativa;
- processamento assíncrono por fonte com Vercel Workflow e progresso persistido no Neon;
- classificação preparatória `CANDIDATO_AUTOMACAO` ou `REVISAO_HUMANA`, sem executar ações críticas automaticamente.

Validação desta versão: **28 testes automatizados aprovados**, importação das oito fontes operacionais validada e deployment de produção sem erros de runtime após a publicação.

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
- Dashboard adaptado ao perfil e limitado aos centros, unidades e propriedades autorizados.
- Filtros por período, centro, direção, NF-e, status e preferência de RT.
- Paginação de 25, 50, 100 ou 250 registros, com ordenação e total filtrado.
- Pendências com diagnóstico, ação recomendada, confiança e exportação filtrada em CSV/XLSX.
- Fila operacional **Regularizar SISDEV** para entradas e saídas, com saldo encontrado, necessidade, falta e saldo projetado.
- Notas fiscais, receitas, movimentações e estoques em visões próprias.
- Cadastros/dimensões derivados dos dados importados.
- Mapeamentos persistentes de produto, propriedade/CNPJ, CNPJ-centro/URE, lote fabricante e regras.
- Exportação CSV e XLSX de relatórios e da fila operacional.
- Autenticação, logout, sessão revogável, cinco perfis e autorização também nas APIs.
- Escopo por centro, unidade e propriedade; administrador possui escopo global.
- Trilha de auditoria para login, exportação, importação, mapeamentos, usuários, configurações e tratamento de pendências.
- Histórico de importações, eventos, progresso, alertas e causa amigável de falha.
- Backup privado das configurações, teste de restauração e restauração administrativa confirmada.

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
| `POST /api/auth/login` | Autentica e cria uma sessão revogável |
| `GET /api/auth/me` | Retorna usuário, perfil, permissões e escopos |
| `POST /api/auth/logout` | Invalida a sessão no servidor |
| `GET/POST /api/auth/users` | Consulta/criação de usuários pelo administrador |
| `PATCH /api/auth/users/{id}` | Perfil, situação, senha e escopos do usuário |
| `POST /api/pending/{id}/actions` | Registra tratamento humano da pendência |
| `POST /api/admin/backups` | Cria snapshot privado de configurações |
| `POST /api/admin/backups/{id}/restore-test` | Valida integridade e legibilidade do snapshot |
| `POST /api/admin/backups/{id}/restore` | Restaura snapshot com confirmação e justificativa |
| `GET/POST /api/settings/rt-preference` | Preferência de RT |
| `GET/POST /api/mappings` | Premissas e mapeamentos |
| `GET /api/health` | Saúde básica da API |

## Configuração

Variáveis obrigatórias na Vercel:

```text
DATABASE_URL=<conexão pooled do Neon>
BLOB_READ_WRITE_TOKEN=<token do Vercel Blob>
SISDEV_SECRET_KEY=<segredo aleatório longo para assinar a sessão>
SISDEV_BOOTSTRAP_ADMIN_EMAIL=<usuário ou e-mail do primeiro administrador>
SISDEV_BOOTSTRAP_ADMIN_PASSWORD=<senha inicial forte>
SISDEV_BOOTSTRAP_ADMIN_NAME=<nome opcional>
```

O administrador inicial só é criado quando ainda não existe nenhum usuário. Depois do primeiro acesso, troque a senha inicial e remova `SISDEV_BOOTSTRAP_ADMIN_PASSWORD` do ambiente. Nunca versionar essas variáveis.

Mantenha `DATABASE_URL_UNPOOLED` somente para migrações administrativas quando disponibilizada pela integração Neon. Para desenvolvimento, `SISDEV_DB_PATH` permite usar um SQLite isolado.

### Banco

Antes da primeira publicação da fila, execute no Neon:

```text
sql/import_jobs.sql
sql/security_access.sql
sql/session_audit_hardening.sql
```

Os scripts são idempotentes e podem ser reaplicados em atualizações de schema. Use conexão direta para a migração e a conexão pooled para a aplicação.

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

## Segurança e governança

- Todas as páginas e APIs operacionais exigem sessão válida; mutações também exigem token CSRF.
- A autorização combina usuário, perfil, permissão e escopo e é validada no backend.
- Senhas usam hash `scrypt`; tentativas falhas são registradas e limitadas temporariamente.
- Cookies são `HttpOnly`, `Secure` em produção e `SameSite=Lax`; logout revoga a sessão no banco.
- A sessão expira após 30 minutos sem atividade e possui limite absoluto de 8 horas, mesmo com uso contínuo.
- Arquivos originais são gravados como privados no Blob e suas URLs não são devolvidas nas APIs.
- Uploads validam extensão, assinatura, ZIP XLSX, presença de macros, tamanho, estrutura, colunas e limites de linhas/colunas.
- CSV e XLSX neutralizam células iniciadas como fórmulas.
- Segredos e credenciais permanecem exclusivamente no backend e em variáveis de ambiente.
- Logs de auditoria resolvem o usuário autenticado em `app_users` antes da gravação e não possuem rota comum de exclusão. Uma indisponibilidade secundária do log é registrada tecnicamente, mas não invalida uma exportação já gerada.
- O backup principal do banco é o point-in-time restore do Neon; o sistema adiciona snapshots privados das configurações e um teste explícito de restauração.

Detalhes operacionais estão em `docs/SECURITY_AND_BACKUP.md`.
