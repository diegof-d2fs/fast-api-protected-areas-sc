-- Colunas de período nas views de reporting: ano e mês prontos para filtro, agregação e
-- relacionamento com uma tabela calendário no Power BI, com o mesmo nome do PRODES (`nr_ano`).
-- As tabelas de produção não mudam. CREATE OR REPLACE VIEW só aceita colunas novas no fim,
-- por isso cada view repete a lista da migração 005 e acrescenta as novas depois de `geom`.
-- FIRMS registra o instante em UTC; o período é calculado na data de Brasília, para que um
-- foco da noite de 31/12 não seja contado no ano seguinte.

CREATE OR REPLACE VIEW reporting.firms_clip AS
SELECT
    id_firms_clip, detection_id, source_product, source_version, source_processing,
    id_uc, id_za_oficial, id_buffer_abrangencia, tipo_cruzamento, acquired_at_utc,
    satellite, instrument, confidence_raw, confidence_scheme, confidence_score,
    confidence_class, publication_rule_version, frp_mw, brightness_kelvin,
    brightness_secondary_kelvin, scan_km, track_km, daynight, detection_type, dt_carga,
    ST_X(geom) AS longitude,
    ST_Y(geom) AS latitude,
    geom,
    (acquired_at_utc AT TIME ZONE 'America/Sao_Paulo')::date AS dt_deteccao_local,
    date_part('year', acquired_at_utc AT TIME ZONE 'America/Sao_Paulo')::int AS nr_ano,
    date_part('month', acquired_at_utc AT TIME ZONE 'America/Sao_Paulo')::int AS nr_mes
FROM public.firms_clip;

CREATE OR REPLACE VIEW reporting.mapbiomas_alerta_clip AS
SELECT
    id_alerta_clip, id_uc, id_za_oficial, id_buffer_abrangencia, tipo_cruzamento,
    id_alerta_original, dt_deteccao, dt_imagem_anterior, dt_imagem_posterior, area_ha,
    ds_bioma, ds_fonte_deteccao, dt_carga,
    ST_X(ST_PointOnSurface(geom)) AS longitude,
    ST_Y(ST_PointOnSurface(geom)) AS latitude,
    geom,
    date_part('year', dt_deteccao)::int AS nr_ano,
    date_part('month', dt_deteccao)::int AS nr_mes
FROM public.mapbiomas_alerta_clip;

CREATE OR REPLACE VIEW reporting.mapbiomas_clip AS
SELECT
    clip.id_mb_clip, clip.id_uc, clip.id_za_oficial, clip.id_buffer_abrangencia,
    clip.aoi_type, clip.collection_code, clip.collection_version, clip.reference_year,
    legend.class_code, legend.name_pt_br AS class_name_pt_br, legend.name_en AS class_name_en,
    legend.color_hex AS class_color_hex, clip.pixel_count, clip.class_area_ha,
    clip.classified_area_ha, clip.aoi_area_ha_geodesic, clip.coverage_ratio,
    clip.area_method, clip.area_method_version, clip.boundary_policy, clip.dt_carga,
    clip.reference_year AS nr_ano
FROM public.mapbiomas_clip AS clip
JOIN public.mapbiomas_legend_class AS legend ON legend.id_legend_class = clip.id_legend_class;
