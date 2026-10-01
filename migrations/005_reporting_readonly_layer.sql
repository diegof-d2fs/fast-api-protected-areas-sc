-- Camada de leitura isolada para consumidores externos (GeoServer, QGIS, Power BI).
-- Consumidores nunca leem tabelas de `public`: só as views de `reporting`, que expõem os
-- produtos publicados sem colunas de identidade de operador nem de correlação interna.
-- As views pertencem ao dono das tabelas, então o papel de leitura precisa apenas de SELECT
-- nelas. `public` mantém USAGE para resolver as funções PostGIS (ST_AsBinary, ST_Extent),
-- sem nenhum SELECT em tabela. O raster MapBiomas é servido pelo GeoServer a partir do COG
-- Gold publicado; `reporting.mapbiomas_raster_asset` informa quais anos estão publicados.
-- Senhas dos logins não ficam neste arquivo: `migrate.py` as aplica a partir do ambiente.

CREATE SCHEMA IF NOT EXISTS reporting;
COMMENT ON SCHEMA reporting IS 'Produtos publicados, somente leitura, para consumidores externos.';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'reporting_readonly') THEN
        CREATE ROLE reporting_readonly NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'geoserver_svc') THEN
        CREATE ROLE geoserver_svc LOGIN PASSWORD NULL CONNECTION LIMIT 20 IN ROLE reporting_readonly;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'powerbi_svc') THEN
        CREATE ROLE powerbi_svc LOGIN PASSWORD NULL CONNECTION LIMIT 10 IN ROLE reporting_readonly;
    END IF;
END $$;

-- Parâmetros de sessão não são herdados do papel de grupo; valem por login.
ALTER ROLE geoserver_svc SET search_path = reporting, public;
ALTER ROLE geoserver_svc SET default_transaction_read_only = on;
ALTER ROLE geoserver_svc SET statement_timeout = '120s';
ALTER ROLE powerbi_svc SET search_path = reporting, public;
ALTER ROLE powerbi_svc SET default_transaction_read_only = on;
ALTER ROLE powerbi_svc SET statement_timeout = '120s';
ALTER ROLE powerbi_svc SET idle_in_transaction_session_timeout = '300s';

CREATE OR REPLACE VIEW reporting.uc AS
SELECT
    id_uc, uc_id, cd_cnuc, wdpa_pid, nm_uc, dt_criacao, ds_ato_legal, ds_grupo,
    ds_categoria, ds_esfera, nm_orgao_gestor, sg_uf, area_total_ha, area_ato_ha,
    situacao, versao_registro, dt_inicio_vigencia, dt_fim_vigencia, atualizado_em,
    ST_X(ST_PointOnSurface(geom)) AS longitude,
    ST_Y(ST_PointOnSurface(geom)) AS latitude,
    geom
FROM public.uc;

CREATE OR REPLACE VIEW reporting.za_oficial AS
SELECT
    id_za_oficial, id_uc, ds_fonte, numero_versao, fl_ativa,
    dt_inicio_vigencia, dt_fim_vigencia, dt_carga,
    ST_X(ST_PointOnSurface(geom)) AS longitude,
    ST_Y(ST_PointOnSurface(geom)) AS latitude,
    geom
FROM public.za_oficial;

CREATE OR REPLACE VIEW reporting.buffer_abrangencia AS
SELECT
    id_buffer_abrangencia, id_uc, ds_fonte, dist_buffer_m, numero_versao, fl_ativa,
    dt_geracao, dt_inicio_vigencia, dt_fim_vigencia, dt_carga,
    ST_X(ST_PointOnSurface(geom)) AS longitude,
    ST_Y(ST_PointOnSurface(geom)) AS latitude,
    geom
FROM public.buffer_abrangencia;

CREATE OR REPLACE VIEW reporting.prodes_clip AS
SELECT
    id_prodes_clip, id_uc, id_za_oficial, id_buffer_abrangencia, tipo_cruzamento,
    id_prodes_original, nr_ano, ds_class_name, area_km2, area_intersecao_km2, dt_carga,
    ST_X(ST_PointOnSurface(geom)) AS longitude,
    ST_Y(ST_PointOnSurface(geom)) AS latitude,
    geom
FROM public.prodes_clip;

CREATE OR REPLACE VIEW reporting.mapbiomas_alerta_clip AS
SELECT
    id_alerta_clip, id_uc, id_za_oficial, id_buffer_abrangencia, tipo_cruzamento,
    id_alerta_original, dt_deteccao, dt_imagem_anterior, dt_imagem_posterior, area_ha,
    ds_bioma, ds_fonte_deteccao, dt_carga,
    ST_X(ST_PointOnSurface(geom)) AS longitude,
    ST_Y(ST_PointOnSurface(geom)) AS latitude,
    geom
FROM public.mapbiomas_alerta_clip;

CREATE OR REPLACE VIEW reporting.firms_clip AS
SELECT
    id_firms_clip, detection_id, source_product, source_version, source_processing,
    id_uc, id_za_oficial, id_buffer_abrangencia, tipo_cruzamento, acquired_at_utc,
    satellite, instrument, confidence_raw, confidence_scheme, confidence_score,
    confidence_class, publication_rule_version, frp_mw, brightness_kelvin,
    brightness_secondary_kelvin, scan_km, track_km, daynight, detection_type, dt_carga,
    ST_X(geom) AS longitude,
    ST_Y(geom) AS latitude,
    geom
FROM public.firms_clip;

CREATE OR REPLACE VIEW reporting.mapbiomas_legend_class AS
SELECT
    id_legend_class, collection_code, collection_version, class_code, parent_class_code,
    hierarchy_level, name_pt_br, name_en, color_hex, is_terminal, is_active
FROM public.mapbiomas_legend_class;

-- Estatística tabular por AOI, com o nome e a cor da classe já resolvidos.
CREATE OR REPLACE VIEW reporting.mapbiomas_clip AS
SELECT
    clip.id_mb_clip, clip.id_uc, clip.id_za_oficial, clip.id_buffer_abrangencia,
    clip.aoi_type, clip.collection_code, clip.collection_version, clip.reference_year,
    legend.class_code, legend.name_pt_br AS class_name_pt_br, legend.name_en AS class_name_en,
    legend.color_hex AS class_color_hex, clip.pixel_count, clip.class_area_ha,
    clip.classified_area_ha, clip.aoi_area_ha_geodesic, clip.coverage_ratio,
    clip.area_method, clip.area_method_version, clip.boundary_policy, clip.dt_carga
FROM public.mapbiomas_clip AS clip
JOIN public.mapbiomas_legend_class AS legend ON legend.id_legend_class = clip.id_legend_class;

CREATE OR REPLACE VIEW reporting.mapbiomas_raster_asset AS
SELECT
    id_raster_asset, collection_code, collection_version, reference_year, coverage_scope,
    storage_key, checksum_sha256, byte_size, media_type, crs_epsg, width_px, height_px,
    band_count, data_type, nodata_value, compression, publication_status, published_at,
    footprint
FROM public.mapbiomas_raster_asset
WHERE publication_status = 'PUBLISHED';

-- Views não expõem chave primária ao catálogo. O GeoServer lê esta tabela (formato GeoTools)
-- para identificar as feições e ordenar a paginação do WFS (startIndex); sem ela, a paginação
-- falha com "Cannot do natural order without a primary key".
CREATE TABLE IF NOT EXISTS reporting.gt_pk_metadata (
    table_schema  VARCHAR(32) NOT NULL,
    table_name    VARCHAR(32) NOT NULL,
    pk_column     VARCHAR(32) NOT NULL,
    pk_column_idx INTEGER,
    pk_policy     VARCHAR(32),
    pk_sequence   VARCHAR(64),
    CONSTRAINT uq_gt_pk_metadata UNIQUE (table_schema, table_name, pk_column),
    CONSTRAINT chk_gt_pk_metadata_policy CHECK (pk_policy IN ('sequence', 'assigned', 'autogenerated'))
);
COMMENT ON TABLE reporting.gt_pk_metadata IS 'Chave de cada view de reporting, lida pelo GeoServer.';

INSERT INTO reporting.gt_pk_metadata (table_schema, table_name, pk_column, pk_column_idx, pk_policy)
VALUES
    ('reporting', 'uc', 'id_uc', 1, 'assigned'),
    ('reporting', 'za_oficial', 'id_za_oficial', 1, 'assigned'),
    ('reporting', 'buffer_abrangencia', 'id_buffer_abrangencia', 1, 'assigned'),
    ('reporting', 'prodes_clip', 'id_prodes_clip', 1, 'assigned'),
    ('reporting', 'mapbiomas_alerta_clip', 'id_alerta_clip', 1, 'assigned'),
    ('reporting', 'firms_clip', 'id_firms_clip', 1, 'assigned'),
    ('reporting', 'mapbiomas_legend_class', 'id_legend_class', 1, 'assigned'),
    ('reporting', 'mapbiomas_clip', 'id_mb_clip', 1, 'assigned'),
    ('reporting', 'mapbiomas_raster_asset', 'id_raster_asset', 1, 'assigned')
ON CONFLICT ON CONSTRAINT uq_gt_pk_metadata DO NOTHING;

REVOKE ALL ON SCHEMA reporting FROM PUBLIC;
GRANT USAGE ON SCHEMA reporting TO reporting_readonly;
REVOKE ALL ON ALL TABLES IN SCHEMA reporting FROM PUBLIC;
GRANT SELECT ON ALL TABLES IN SCHEMA reporting TO reporting_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA reporting GRANT SELECT ON TABLES TO reporting_readonly;
REVOKE CREATE ON SCHEMA public FROM reporting_readonly;
