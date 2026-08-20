# Implantação no Vercel

## O que está pronto

- `api/index.py` exporta uma aplicação Flask, reconhecida pelo runtime Python
  do Vercel.
- `vercel.json` encaminha interface e API para a mesma função.
- Python 3.12 está fixado em `.python-version`.
- A função tem 1 GB de memória e até 300 segundos para importar as planilhas.

## Recursos obrigatórios antes da produção

O armazenamento local de uma Vercel Function não é persistente. Portanto, não
use `dados/` e `banco/sisdev_auditor.sqlite` em produção. Crie e conecte:

1. **Vercel Blob privado** para as planilhas enviadas.
2. **Neon Postgres** (ou Supabase Postgres) para importações, conciliações,
   configurações e histórico.
3. Um provedor de autenticação antes de liberar upload a usuários.

As variáveis esperadas estão em `.env.example`. Nunca publique os valores.

## Próximos passos no Vercel

1. Instale o Vercel CLI: `npm i -g vercel`.
2. Faça login: `vercel login`.
3. Na pasta do projeto, execute `vercel link`.
4. No Marketplace, adicione Neon e Vercel Blob ao projeto.
5. Configure as variáveis de ambiente e execute `vercel env pull .env.local`.
6. Faça o deploy com `vercel --prod`.

## Atenção

Esta alteração torna a interface e as rotas HTTP compatíveis com Functions.
A migração de SQLite e da pasta local de uploads para Postgres/Blob ainda
depende da criação dos recursos da conta Vercel e das credenciais de ambiente.
Não faça um deploy operacional antes dessa etapa, pois os dados poderiam ser
perdidos entre execuções.
