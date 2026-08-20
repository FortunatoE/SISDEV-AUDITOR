# SISDEV AUDITOR

Aplicação de auditoria e conciliação de dados SAP, SISDEV e Agrotis.

## Envio de arquivos

No menu **Importar arquivos**, o usuário envia diretamente as fontes abaixo:

- SAP — entradas (`.xlsx`)
- SAP — saídas (`.xlsx`)
- SAP — estoque MB52 (`.xlsx`)
- SISDEV — relatório de movimentações (`.xlsx`)
- Agrotis — receitas emitidas (`.xls` ou `.xlsx`)

Cada envio substitui apenas a sua própria fonte, com limite de 50 MB. Ao fim,
clique em **Processar arquivos enviados** para executar a conciliação.

## Executar localmente

Instale as dependências e inicie o servidor:

```powershell
python -m pip install -r requirements.txt
python -B src\main.py
```

Abra `http://127.0.0.1:8765`.

## Hospedar na nuvem

O servidor respeita a variável `PORT`, adotada por provedores como Render,
Railway e Fly.io. O arquivo `Procfile` contém o comando de inicialização.

Para dados persistirem entre reinicializações, configure um volume persistente
no provedor e monte-o na pasta `dados/` e no arquivo SQLite em `banco/`. Antes
de disponibilizar a aplicação para terceiros, adicione autenticação e HTTPS:
as planilhas podem conter informações operacionais e pessoais.

## Regras iniciais aprovadas

- SAP é a referência operacional de quantidade.
- Lote fabricante é prioritário; divergências com o lote SAP são alertadas.
- Estoque é comparado por centro; diferença `SISDEV - SAP` positiva indica
  necessidade de saída e negativa, necessidade de entrada.
- Receita candidata: mesmo dia da NF (com exceção D-1), produto mapeado,
  quantidade e unidade compatíveis. Múltiplos candidatos ficam em revisão,
  sem vínculo definitivo.
