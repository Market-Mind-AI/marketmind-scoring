-- MarketMind Core — cria um usuário read-only para o Core consultar as
-- tabelas de outro serviço diretamente (ver marketmind-core/README.md,
-- seção "Conectividade entre serviços").
--
-- Rode este script UMA VEZ contra CADA UMA das 4 bases dos serviços de
-- coleta, conectado como o usuário admin que já existe naquele serviço
-- (o usuário "marketmind" já definido no .env daquele serviço). Exemplo:
--
--   psql "postgresql://marketmind:admin@localhost:5432/marketmind_pytrends" \
--        -f scripts/create_readonly_roles.sql
--
-- Repita trocando a porta/nome do banco para:
--   marketmind_worldbank              (porta DATABASE_EXTERNAL_PORT daquele serviço)
--   marketmind_oecd                   (porta DATABASE_PORT_HOST daquele serviço)
--   marketmind_united_nations_data    (porta DATABASE_EXTERNAL_PORT daquele serviço)
--
-- A senha precisa bater com PYTRENDS_DB_PASSWORD / WORLDBANK_DB_PASSWORD /
-- OECD_DB_PASSWORD / UNDATA_DB_PASSWORD no .env do marketmind-core.
--
-- Ela vem de uma variável do psql em vez de estar fixa aqui. Antes o script
-- criava a role com 'admin' embutido, o que só funcionava no docker-compose
-- local: na Azure a senha é gerada e guardada no Key Vault
-- (postgres-readonly-password), então o valor fixo criava uma role com a senha
-- errada e o Core falhava a conexão sem nenhum erro que apontasse pra cá.
--
-- Local:
--   psql "postgresql://marketmind:admin@localhost:5432/marketmind_pytrends" \
--        -v readonly_password=admin -f scripts/create_readonly_roles.sql
--
-- Azure: o servidor só aceita conexão do IP de saída do AKS, então rode de
-- dentro do cluster. Note que role no PostgreSQL é objeto de cluster, não de
-- banco — o bloco DO roda uma vez só, os GRANTs é que são por banco.

-- Sem o guard, uma variável não definida faz `:'readonly_password'` virar uma
-- string vazia — a role seria criada com senha em branco, silenciosamente.
\if :{?readonly_password}
\else
    \echo 'ERRO: defina a senha com  -v readonly_password=<senha>'
    -- RAISE em vez de só `\quit`: o `\quit` do psql não aceita código de saída
    -- e sempre encerra com status 0, então quem chamasse este script dentro de
    -- outro não veria falha nenhuma. Com ON_ERROR_STOP=1 (usado nos exemplos
    -- acima) o RAISE encerra com status diferente de zero.
    DO $$ BEGIN RAISE EXCEPTION 'readonly_password nao definida'; END $$;
    \quit
\endif

-- Gerado com format() + \gexec em vez de um bloco DO $$ ... $$.
--
-- O psql não interpola variáveis dentro de dollar-quoting: `:'readonly_password'`
-- dentro de um $$...$$ chegaria literalmente assim ao servidor, e a role
-- nasceria com a senha errada. Montando o comando fora das aspas, a
-- substituição acontece, e %L cuida do escape.
--
-- ALTER quando já existe: idempotente também ao repontar um ambiente pra um
-- banco novo, onde a role existe mas com outra senha.
SELECT format(
    CASE
        WHEN EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'marketmind_readonly')
        THEN 'ALTER ROLE marketmind_readonly WITH LOGIN PASSWORD %L'
        ELSE 'CREATE ROLE marketmind_readonly WITH LOGIN PASSWORD %L'
    END,
    :'readonly_password'
) \gexec

GRANT CONNECT ON DATABASE :"DBNAME" TO marketmind_readonly;
GRANT USAGE ON SCHEMA public TO marketmind_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO marketmind_readonly;

-- Garante que tabelas criadas por migrations futuras daquele serviço também
-- fiquem legíveis, sem precisar rodar este script de novo.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO marketmind_readonly;
