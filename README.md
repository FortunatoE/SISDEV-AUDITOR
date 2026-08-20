# SISDEV AUDITOR

Aplicação para auditoria, conciliação e preparação operacional de lançamentos
entre SAP, SISDEV e Agrotis. O objetivo é identificar documentos pendentes ou
divergentes, priorizar o lote fabricante e entregar uma fila de regularização
com os dados necessários para lançamento no SISDEV.

> O sistema não faz lançamentos automáticos no SISDEV. Ele organiza os dados,
> aponta divergências e prepara a informação para conferência humana.

## Funcionalidades

### Dashboard e filtros

- Indicadores por documento SAP distinto: analisados, corretos, pendentes,
  divergentes, não lançados, eficácia e aderência de lançamentos.
- Filtros por período, centro e direção do movimento.
- Gráficos calculados a partir do resultado real da última importação.
- Alerta de duplicidades SISDEV por chave operacional.

### Visões analíticas

- **Pendências:** análise detalhada de documentos não corretos, incluindo
  diagnóstico, confiança, direção, material, lote SAP, lote fabricante, lote
  SISDEV e quantidades comparadas.
- **Análises, notas fiscais, movimentações, estoques, receitas, cadastros e
  relatórios:** dados organizados por área operacional.
- **Estoque:** comparação por centro com quantidade SAP, quantidade SISDEV e
  diferença `SISDEV - SAP`.
- **Receitas:** dados por emissão, produto, volume, RT, ART, receituário,
  diagnóstico, propriedade e CNPJ quando mapeado.

### Fila “Regularizar SISDEV”

Nova fila voltada apenas a notas SAP sem lançamento no SISDEV.

- Separa entradas e saídas.
- Mostra data, CNPJ, NF, série, produto, lote fabricante, lote SAP,
  quantidade, unidade e URE/centro.
- Para saídas, tenta vincular receita pelo produto e pela janela de emissão
  **D ou D-1** em relação à data do documento.
- Exibe situação operacional: receita sugerida, receitas múltiplas, sem
  receita ou preenchimento de embalagem pendente.
- Quando houver receita única, mostra receituário, ART, RT, cultura,
  diagnóstico, dose, tipo de dosagem, área da receita e área calculada.
- A preferência de RT é configurável no próprio sistema e é persistida no
  SQLite. O padrão inicial é **Karla Danielly Garcia de Lima**. Em caso de
  receitas múltiplas, uma receita do RT preferido é priorizada.

### Dose e área

O cálculo de área usa a dose normalizada:

- `g/ha` é convertido para `kg/ha` (`÷ 1.000`).
- `mL/ha` é convertido para `L/ha` (`÷ 1.000`).
- `Área = Quantidade do lote ÷ Dose normalizada`.

Exemplo: `40 L ÷ 0,06 L/ha = 666,67 ha`.

### Exportação

- Relatório de validação em CSV e XLSX, preservando a estrutura da guia
  **Validação** de `Acompanhamento SISDEV.xlsx`.
- Exportação CSV/XLSX da fila **Regularizar SISDEV** para notas de saída com:
  data, CNPJ, NF, série, produto, lote, quantidade, volume/quantidade de
  embalagem, receituário, ART, RT, cultura, diagnóstico, URE, dose e área.

### Importação de arquivos

A guia **Importar arquivos** recebe planilhas de até 50 MB, substituindo apenas
a fonte correspondente:

| Fonte | Arquivo esperado |
| --- | --- |
| SAP — Entradas | `entrada_sisdev.xlsx` |
| SAP — Saídas | `saída_sisdev.xlsx` |
| SAP — Estoque | `MB52.xlsx` |
| SISDEV — Movimentações | Relatório de análise de movimentação |
| Agrotis — Receitas | `ReceitasEmitidas.xls` ou `.xlsx` |

Após o envio, o botão **Processar arquivos enviados** cria uma nova execução de
importação e conciliação.

## Regras de conciliação

- SAP é a referência operacional de quantidade.
- O lote fabricante é prioritário; o lote SAP permanece como referência.
- A comparação de estoque é por centro.
- Diferença positiva `SISDEV - SAP` indica necessidade de saída; diferença
  negativa indica necessidade de entrada.
- Uma saída é considerada conciliável com receita apenas com produto compatível
  e emissão no mesmo dia da NF ou em D-1.
- Quantidades SAP e SISDEV são comparadas considerando embalagens × volume.

## Estrutura técnica

```text
src/
├── main.py                    # ponto de entrada do servidor
├── auditor/
│   ├── server.py              # HTTP, APIs, upload e exportação
│   ├── engine.py              # importação, conciliação e regras operacionais
│   ├── database.py            # schema SQLite e configurações persistentes
│   └── normalization.py       # normalização de texto, lote, documento e números
└── web/
    ├── index.html             # estrutura da interface
    ├── app.js                 # páginas, filtros, upload e exportações
    ├── style.css / fixes.css  # estilo do dashboard
    └── assets/                # logo da Boa Esperança

dados/                         # fontes enviadas pelo usuário (ignorado pelo Git)
banco/sisdev_auditor.sqlite    # banco local SQLite (ignorado pelo Git)
```

### Backend

- Python com `ThreadingHTTPServer` e API JSON sem dependência de framework.
- Pandas para leitura das planilhas Excel.
- SQLite para histórico de importações, registros de fonte, movimentos SAP,
  movimentos SISDEV, conciliações, alertas e configurações da aplicação.
- Arquivos recebidos via `multipart/form-data`, com lista de fontes permitidas
  e validação de extensão `.xlsx`/`.xls`.
- Exportação XLSX gerada sem dependência adicional, usando pacote ZIP/XML.

### Principais rotas

| Rota | Função |
| --- | --- |
| `GET /api/dashboard` | KPIs, filtros, gráficos e pendências recentes |
| `GET /api/page/{pagina}` | Dados de cada guia |
| `POST /api/upload` | Recebimento de uma planilha por fonte |
| `POST /api/import` | Processamento e conciliação dos arquivos |
| `GET /api/export/{csv|xlsx}/{pagina}` | Exportação operacional |
| `GET/POST /api/settings/rt-preference` | Consulta e configuração da preferência de RT |

## Executar localmente

```powershell
python -m pip install -r requirements.txt
python -B src\main.py
```

Abra `http://127.0.0.1:8765`.

## Preparação para nuvem

O servidor usa `PORT`, compatível com Render, Railway e Fly.io. O `Procfile`
contém o comando de inicialização:

```text
web: python -B src/main.py
```

Para produção, configure armazenamento persistente para `dados/` e `banco/`,
HTTPS, autenticação e controle de acesso. As planilhas podem conter dados
operacionais e pessoais e não devem ser publicadas no repositório.

## Dados protegidos pelo Git

O `.gitignore` exclui planilhas operacionais, banco SQLite, resultados, logs,
configurações locais e arquivos temporários do Excel. O repositório contém
somente código, documentação e ativos visuais necessários à aplicação.
