-- Login fixo de leitura para o grupo do laboratório (projeto de extensão), usado no Power BI e
-- em clientes SQL como o DBeaver. Mesmo isolamento da migração 005: só as views de `reporting`,
-- pelo papel `reporting_readonly`. Participantes de oficina usam o login do modo eventos.
-- A senha não fica aqui: `migrate.py` a aplica a partir do ambiente; sem ela, o login segue
-- bloqueado.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lab_svc') THEN
        CREATE ROLE lab_svc LOGIN PASSWORD NULL CONNECTION LIMIT 12 IN ROLE reporting_readonly;
    END IF;
END $$;

-- Parâmetros de sessão não são herdados do papel de grupo; valem por login.
ALTER ROLE lab_svc SET search_path = reporting, public;
ALTER ROLE lab_svc SET default_transaction_read_only = on;
ALTER ROLE lab_svc SET statement_timeout = '220s';
ALTER ROLE lab_svc SET idle_in_transaction_session_timeout = '300s';
