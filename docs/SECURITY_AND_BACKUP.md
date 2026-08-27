# Segurança, acesso e backup

## Autenticação

O backend Flask valida a sessão em todas as rotas `/api/*`, exceto saúde e login. A senha é armazenada com `scrypt`; a sessão usa um token aleatório, persistido no banco somente como SHA-256. Requisições de escrita também exigem token CSRF.

O primeiro administrador é criado exclusivamente por variáveis de ambiente do servidor:

- `SISDEV_SECRET_KEY`: segredo longo e aleatório para assinatura da sessão;
- `SISDEV_BOOTSTRAP_ADMIN_EMAIL` (usuário ou e-mail de acesso);
- `SISDEV_BOOTSTRAP_ADMIN_PASSWORD` (mínimo de 10 caracteres);
- `SISDEV_BOOTSTRAP_ADMIN_NAME` (opcional).

Após o primeiro acesso e a confirmação de que o administrador existe, remova `SISDEV_BOOTSTRAP_ADMIN_PASSWORD` do ambiente e publique novamente. O código nunca contém senha padrão.

## Perfis e escopos

Os perfis são `ADMINISTRADOR`, `GESTOR`, `AUDITOR`, `OPERADOR` e `CONSULTA`. Permissões são verificadas no backend. Usuários não administradores recebem escopos `CENTER`, `UNIT` e/ou `PROPERTY`; consultas fora desses escopos retornam HTTP 403.

## Blob

Uploads e backups são gravados com `access="private"`. Caminhos e URLs privadas não são devolvidos ao navegador. Somente o worker e os endpoints administrativos autorizados leem os objetos usando `BLOB_READ_WRITE_TOKEN` no servidor.

## Backup e restauração

Há duas camadas complementares:

1. **Banco completo**: usar a restauração instantânea/PITR do Neon ou `pg_dump`/`pg_restore` com `DATABASE_URL_UNPOOLED`. Esta é a camada que recupera importações e trilha de auditoria.
2. **Configuração da aplicação**: `POST /api/admin/backups` grava `app_settings` e `reconciliation_mappings` em Blob privado e registra o checksum em `backup_registry`.

Antes de uma restauração de configuração, execute `POST /api/admin/backups/{id}/restore-test`. O endpoint baixa o objeto privado, valida SHA-256 e estrutura JSON. A restauração efetiva exige `confirm: true` e uma justificativa, e fica registrada na trilha de auditoria.

Migrações devem usar conexão direta, nunca o pooler. Em produção, teste primeiro em uma branch Neon criada a partir do estado atual e valide o diff antes de aplicar à branch principal.
