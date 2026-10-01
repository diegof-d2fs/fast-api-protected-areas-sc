-- Precedência da ZA oficial sobre o Buffer de Abrangência e histórico uniforme das duas zonas.
--
-- Invariante de domínio por UC:
--   * no máximo uma ZA oficial ativa;
--   * no máximo um Buffer de Abrangência ativo;
--   * ZA oficial e Buffer de Abrangência nunca ficam ativos ao mesmo tempo.
--
-- Sem BEGIN/COMMIT próprios: o runner (`app/infrastructure/persistence/migrate.py`) já executa
-- cada arquivo dentro de uma transação; um BEGIN/COMMIT textual aqui dentro conflita com o
-- controle de savepoint do psycopg3 (`InvalidSavepointSpecification`) quando aplicado por ele.

ALTER TABLE public.za_oficial
    ADD COLUMN IF NOT EXISTS numero_versao BIGINT NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS motivo TEXT,
    ADD COLUMN IF NOT EXISTS ator VARCHAR(255),
    ADD COLUMN IF NOT EXISTS correlation_id VARCHAR(128),
    ADD COLUMN IF NOT EXISTS import_id UUID,
    ADD COLUMN IF NOT EXISTS dag_run_id VARCHAR(255);

CREATE UNIQUE INDEX IF NOT EXISTS uq_za_oficial_versao_por_uc
    ON public.za_oficial (id_uc, numero_versao);

CREATE UNIQUE INDEX IF NOT EXISTS uq_za_oficial_ativa_por_uc
    ON public.za_oficial (id_uc) WHERE fl_ativa = TRUE;

CREATE UNIQUE INDEX IF NOT EXISTS uq_buffer_abrangencia_ativo_por_uc
    ON public.buffer_abrangencia (id_uc) WHERE fl_ativa = TRUE;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_za_oficial_periodo'
          AND conrelid = 'public.za_oficial'::regclass
    ) THEN
        ALTER TABLE public.za_oficial
            ADD CONSTRAINT ck_za_oficial_periodo CHECK (
                dt_fim_vigencia IS NULL OR dt_fim_vigencia >= dt_inicio_vigencia
            );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_buffer_abrangencia_periodo'
          AND conrelid = 'public.buffer_abrangencia'::regclass
    ) THEN
        ALTER TABLE public.buffer_abrangencia
            ADD CONSTRAINT ck_buffer_abrangencia_periodo CHECK (
                dt_fim_vigencia IS NULL OR dt_fim_vigencia >= dt_inicio_vigencia
            );
    END IF;
END $$;

CREATE OR REPLACE FUNCTION public.enforce_uc_active_zone_exclusivity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.fl_ativa IS DISTINCT FROM TRUE THEN
        RETURN NEW;
    END IF;

    -- As duas tabelas usam a mesma linha-pai como trava de serialização.
    PERFORM 1
    FROM public.uc
    WHERE id_uc = NEW.id_uc
    FOR UPDATE;

    IF TG_TABLE_NAME = 'za_oficial' AND EXISTS (
        SELECT 1
        FROM public.buffer_abrangencia
        WHERE id_uc = NEW.id_uc
          AND fl_ativa = TRUE
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = format(
                'UC %s já possui Buffer de Abrangência ativo; encerre-o na mesma transação antes de ativar a ZA oficial.',
                NEW.id_uc
            ),
            CONSTRAINT = 'ck_uc_active_zone_exclusivity';
    END IF;

    IF TG_TABLE_NAME = 'buffer_abrangencia' AND EXISTS (
        SELECT 1
        FROM public.za_oficial
        WHERE id_uc = NEW.id_uc
          AND fl_ativa = TRUE
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = format(
                'UC %s já possui ZA oficial ativa; não é permitido ativar um Buffer de Abrangência.',
                NEW.id_uc
            ),
            CONSTRAINT = 'ck_uc_active_zone_exclusivity';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_za_oficial_active_zone_exclusivity ON public.za_oficial;
CREATE TRIGGER trg_za_oficial_active_zone_exclusivity
BEFORE INSERT OR UPDATE OF id_uc, fl_ativa
ON public.za_oficial
FOR EACH ROW
WHEN (NEW.fl_ativa = TRUE)
EXECUTE FUNCTION public.enforce_uc_active_zone_exclusivity();

DROP TRIGGER IF EXISTS trg_buffer_abrangencia_active_zone_exclusivity ON public.buffer_abrangencia;
CREATE TRIGGER trg_buffer_abrangencia_active_zone_exclusivity
BEFORE INSERT OR UPDATE OF id_uc, fl_ativa
ON public.buffer_abrangencia
FOR EACH ROW
WHEN (NEW.fl_ativa = TRUE)
EXECUTE FUNCTION public.enforce_uc_active_zone_exclusivity();

COMMENT ON FUNCTION public.enforce_uc_active_zone_exclusivity() IS
    'Impede que uma UC mantenha ZA oficial e Buffer de Abrangência simultaneamente ativos; a linha de UC serializa concorrência.';
