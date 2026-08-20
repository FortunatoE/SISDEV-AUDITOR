# SISDEV AUDITOR

Aplicativo local de auditoria e conciliação SAP × SISDEV × AGROTIS.

## Executar

No PowerShell, a partir desta pasta:

```powershell
& 'C:\Users\Emmanuel Fortunato\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' src\main.py
```

Abra `http://127.0.0.1:8765`. O botão **Atualizar dados** importa as fontes da
pasta `dados/`, preserva cada linha original no SQLite e executa a conciliação.
O aplicativo não realiza lançamentos no SISDEV.

## Regras iniciais aprovadas

- SAP é a referência operacional de quantidade.
- Lote fabricante é prioritário; divergências com o lote SAP são alertadas.
- Estoque é comparado por centro; diferença `SISDEV - SAP` positiva indica
  necessidade de saída e negativa, necessidade de entrada.
- Receita candidata: mesmo dia da NF (com exceção D-1), produto mapeado,
  quantidade e unidade compatíveis. Múltiplos candidatos ficam em revisão,
  sem vínculo definitivo.
