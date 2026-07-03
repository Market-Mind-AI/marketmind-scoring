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
-- A senha abaixo precisa bater com PYTRENDS_DB_PASSWORD / WORLDBANK_DB_PASSWORD /
-- OECD_DB_PASSWORD / UNDATA_DB_PASSWORD no .env do marketmind-core.

DO
$$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'marketmind_readonly') THEN
        CREATE ROLE marketmind_readonly WITH LOGIN PASSWORD 'admin';
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO marketmind_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO marketmind_readonly;

-- Garante que tabelas criadas por migrations futuras daquele serviço também
-- fiquem legíveis, sem precisar rodar este script de novo.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO marketmind_readonly;
