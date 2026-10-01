-- Histórico não destrutivo para mutações dirigidas pela API/Bronze/Airflow.
-- Mantém public.uc como snapshot atual e usa tabelas/versionamento para auditoria.

CREATE UNIQUE INDEX IF NOT EXISTS uq_cadastral_event_idempotency
    ON public.cadastral_event (idempotency_key);

ALTER TABLE public.buffer_abrangencia
    ADD COLUMN IF NOT EXISTS numero_versao BIGINT NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS dt_inicio_vigencia DATE NOT NULL DEFAULT CURRENT_DATE,
    ADD COLUMN IF NOT EXISTS dt_fim_vigencia DATE,
    ADD COLUMN IF NOT EXISTS motivo TEXT,
    ADD COLUMN IF NOT EXISTS ator VARCHAR(255),
    ADD COLUMN IF NOT EXISTS correlation_id VARCHAR(128),
    ADD COLUMN IF NOT EXISTS import_id UUID,
    ADD COLUMN IF NOT EXISTS dag_run_id VARCHAR(255);

CREATE UNIQUE INDEX IF NOT EXISTS uq_buffer_abrangencia_versao_por_uc
    ON public.buffer_abrangencia (id_uc, numero_versao);

CREATE UNIQUE INDEX IF NOT EXISTS uq_prodes_associacao_temporal
    ON public.prodes_clip (
        id_uc,
        COALESCE(id_za_oficial, 0),
        COALESCE(id_buffer_abrangencia, 0),
        COALESCE(tipo_cruzamento, ''),
        id_prodes_original
    );

CREATE UNIQUE INDEX IF NOT EXISTS uq_mapbiomas_alerta_associacao_temporal
    ON public.mapbiomas_alerta_clip (
        id_uc,
        COALESCE(id_za_oficial, 0),
        COALESCE(id_buffer_abrangencia, 0),
        COALESCE(tipo_cruzamento, ''),
        id_alerta_original
    );
