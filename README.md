# Protected Areas SC API

API REST do TCC 3 para receber, validar, publicar e acompanhar importações geoespaciais de Unidades de Conservação (`uc`) e Zonas de Amortecimento oficiais (`za_oficial`). o Buffer de Abrangência não é aceito por upload: é derivado da UC pelo pipeline.

## Estado atual

A versão 0.5.0 oferece cadastro poligonal e pontual, lote UC+ZA, atualização versionada,
extinção lógica, substituição de ponto por polígono e substituição de Buffer de Abrangência por ZA oficial pelo
fluxo integrado:

- upload multipart com resposta `202 Accepted`;
- persistência local do estado em SQLite;
- original em quarentena até o aceite;
- armazenamento de aceitos por domínio, data e `import_id`;
- artefato canônico GeoJSON em EPSG:4674;
- manifesto JSON e relatório de validação;
- idempotência por `Idempotency-Key` e checksum SHA-256;
- erros HTTP em `application/problem+json`;
- logs JSON com correlation ID;
- Swagger em `/docs` e OpenAPI em `/openapi.json`.
- publicação imutável na Bronze e disparo idempotente do Airflow;
- `operation=create` com proteção contra UC repetida;
- `UC-CW02`: a UC pontual permanece `Point` no cadastro e o Buffer de Abrangência de 3 km é derivado pelo Airflow;
- `operation=update` com concorrência otimista por `expected_version`;
- snapshot atual preservando `id_uc`, histórico em `uc_geometry_version` e `cadastral_event`;
- versionamento do Buffer de Abrangência e reprocessamento de `DAG_PRODES`, `DAG_MAPBIOMAS_ALERTA` e `DAG_MAPBIOMAS`.
- `operation=replace_buffer_abrangencia` com bloqueio da UC, encerramento do Buffer de Abrangência, ativação da ZA e auditoria na
  mesma transação;
- precedência garantida no PostGIS: uma UC não pode manter ZA oficial e Buffer de Abrangência simultaneamente
  ativas;
- autenticação por sessão em cookie `httpOnly` (Argon2, bloqueio por força bruta, gestão de
  contas por um administrador) protegendo todas as rotas de `/api/v1/imports` e `/api/v1/ucs`.

`POST /api/v1/imports/{import_id}/publish` publica um lote imutável dirigido por manifesto na
Bronze e dispara o Airflow de forma idempotente. Para UC, a cadeia é `DAG_UCS → DAG_ZA_BUFFER →`
DAGs temáticas implementadas. O estado fica `PROCESSING` até a consulta confirmar `SUCCEEDED` ou
`FAILED`. A API não grava diretamente uma UC no PostGIS.

## Contrato REST

| Método e rota | Finalidade |
|---|---|
| `POST /api/v1/imports` | Enviar arquivo e criar uma importação geoespacial |
| `GET /api/v1/imports/{import_id}` | Consultar estado e artefatos da importação |
| `GET /api/v1/imports/{import_id}/validation` | Consultar erros, advertências, CRS e métricas |
| `POST /api/v1/imports/{import_id}/publish` | Publicar na Bronze e iniciar o processamento assíncrono |
| `GET /api/v1/ucs/{id}` | Consultar cadastro e geometria vigente diretamente no PostGIS |
| `GET /api/v1/ucs?cd_cnuc=...` ou `?wdpa_pid=...` | Buscar uma UC e obter a versão vigente |
| `POST /api/v1/ucs/{id}/extinguish` | Criar comando de extinção; publicar pela importação retornada |
| `POST /api/v1/import-batches` | Reservar duas importações aceitas como lote UC+ZA |
| `GET /api/v1/import-batches/{id}` | Acompanhar o lote |
| `POST /api/v1/import-batches/{id}/publish` | Publicar lote atômico em `DAG_UC_ZA` |
| `GET /api/v1/ucs/{id}/buffer-abrangencia` | Consultar Buffer de Abrangência ativo e os históricos sem disparar Airflow |
| `POST /api/v1/auth/login` | Autenticar e abrir uma sessão (cookie `httpOnly`) |
| `POST /api/v1/auth/logout` | Encerrar a sessão atual |
| `GET /api/v1/auth/me` | Consultar o usuário da sessão atual |
| `POST /api/v1/auth/users` | Criar uma conta (somente administrador) |
| `GET /api/v1/auth/users` | Listar contas (somente administrador) |
| `PATCH /api/v1/auth/users/{id}` | Ativar ou desativar uma conta (somente administrador) |
| `GET /api/v1/health/live` | Liveness do processo HTTP |
| `GET /api/v1/health/ready` | Readiness de storage, SQLite, PostGIS, Bronze e Airflow |

Todas as rotas de `imports`, `import-batches` e `ucs` acima exigem sessão válida (`Depends(get_current_user)`); as
rotas de `auth` são o único jeito de obter essa sessão. `docs/GUIA_TESTES_POSTMAN.md`, seção 3.1,
descreve o passo de login e o bootstrap do primeiro administrador.

O nome público `imports` representa o ciclo completo de ingresso, validação e publicação. Não há
`POST /api/v1/ucs`: a coleção de UCs é somente leitura na API. Todas as inclusões e alterações
geoespaciais seguem um único caminho de escrita:

```text
API → Bronze → Airflow → PostGIS → derivados e cruzamentos temáticos
```

Assim, uma UC nunca aparece no banco de domínio sem participar dos processamentos espaciais do
pipeline. Os sete casos do TCC 3 serão modelados como importações/comandos assíncronos, sem um
segundo escritor concorrente no PostGIS.

## Ciclo de vida de UC e zonas

A ZA oficial é opcional no cadastro inicial:

- UC poligonal ou pontual sem ZA: a importação segue normalmente e o Airflow gera um Buffer de Abrangência;
- UC e ZA novas disponíveis juntas: usam um `import-batch` atômico, com
  dois arquivos validados e uma única confirmação no PostGIS;
- ponto substituído posteriormente por polígono: `UC-CW06`, preservando histórico;
- ZA oficial recebida posteriormente: `UC-CW07`, ativando a ZA e encerrando o Buffer de Abrangência anterior.

Há dois modos controlados para uma ZA oficial posterior. Uma nova ZA enviada pela API usa
`za_oficial/replace_buffer_abrangencia`, com `uc_identifier` e `reason`. Além disso, uma ZA autoritativa que já
esteja na Bronze legada, identificada por `ds_fonte`, pode ser descoberta pela execução agendada
da `DAG_ZA_BUFFER` quando sua UC passar a existir. Nesse caso esperado, o pipeline associa a ZA,
encerra o Buffer de Abrangência vigente e reprocessa as bases temáticas. A descoberta espacial legada não é usada
nas execuções dirigidas pela API, que exigem identidade explícita.

O batch UC+ZA não tornará a ZA obrigatória e não será usado para a complementação posterior de
uma UC já existente. O `create` isolado de ZA continua bloqueado porque não expressa nem o lote
atômico UC+ZA nova nem a transição auditável `replace_buffer_abrangencia` de uma UC existente.

O roteiro completo para cadastro, consultas, idempotência, erros e importação de arquivos pelo Postman está em [`docs/GUIA_TESTES_POSTMAN.md`](docs/GUIA_TESTES_POSTMAN.md).

Antes de iniciar uma versão nova da API contra um banco existente, aplique as migrações aditivas:

```powershell
docker compose --profile tools run --rm api-migrate
```

## Formatos e metadados

| Domínio | Geometrias | Metadados mínimos |
|---|---|---|
| `uc/create` | uma ou mais features Point, Polygon ou MultiPolygon | `source`; cada feature do lote deve possuir nome e identidade forte próprias |
| `uc/update` | exatamente um Point, Polygon ou MultiPolygon | `source`, `reason`, `expected_version` e `official_identifier`, `cd_cnuc` ou `wdpa_pid` |
| `za_oficial/replace_buffer_abrangencia` | exatamente um Polygon ou MultiPolygon | `source`, `uc_identifier` e `reason` |

Formatos aceitos:

- Shapefile somente em ZIP, com `.shp`, `.shx`, `.dbf` e `.prj` obrigatórios e `.cpg` recomendado;
- GeoJSON `.geojson` ou `.json`; o CRS deve estar no membro `crs` ou em `metadata.source_crs`;
- KML `.kml`, interpretado em EPSG:4326 conforme o formato;
- exatamente um dataset por requisição.

`uc/create` aceita uma ou mais UCs no mesmo Shapefile ou GeoJSON. O modo padrão
`duplicate_policy=reject_batch` preserva atomicidade e rejeita o lote inteiro quando alguma UC já
existe. `duplicate_policy=skip_duplicates` publica somente as feições inéditas; o original é
preservado e `batch_items`, `duplicate_matches` e o manifesto registram as feições ignoradas.
`uc/update` permanece estritamente unitário e versionado.

A API rejeita geometria inválida por padrão. `repair_geometry=true` habilita tentativa auditada de correção, limitada a 1% de variação relativa de área por padrão.

## Execução local

Requer Python 3.11 ou 3.12.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Acesse:

- Swagger: `http://localhost:8000/docs`
- Health: `http://localhost:8000/api/v1/health/ready`

Exemplo com GeoJSON:

```powershell
curl.exe -X POST "http://localhost:8000/api/v1/imports" `
  -H "Idempotency-Key: demonstracao-uc-001" `
  -H "X-Correlation-ID: tcc-demo-001" `
  -F "domain=uc" `
  -F "operation=create" `
  -F "duplicate_policy=reject_batch" `
  -F 'metadata={"source":"demonstração","name":"UC de exemplo","source_crs":"EPSG:4674"}' `
  -F "repair_geometry=false" `
  -F "file=@tests/fixtures/geospatial/valid_uc.geojson;type=application/geo+json"
```

## Docker Compose

```powershell
docker compose up --build -d
docker compose ps
```

O volume `./data:/app/data` preserva originais, manifestos, canônicos e o SQLite após reinícios. A imagem executa com usuário não privilegiado e um único worker. O SQLite registra o ciclo operacional das importações; o PostGIS permanece como banco geoespacial de domínio escrito pelo pipeline.

## Testes e qualidade

```powershell
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m pytest -q
docker compose --profile test run --build --rm api-test
```

A suíte da API cobre os três formatos, segurança ZIP, CRS, regras por domínio,
idempotência, atualização, `replace_buffer_abrangencia` e contrato HTTP. No pipeline, nove testes cobrem
manifesto, duplicidade, UC pontual, create/update, precedência ZA/Buffer de Abrangência, rollback, auditoria,
restrição no PostGIS e replay idempotente.

## Armazenamento

```text
data/
  submissions.sqlite3
  quarantine/{import_id}/original/{server_filename}
  accepted/{domain}/ingestion_date=YYYY-MM-DD/import_id={id}/
    original/{server_filename}
    canonical/data.geojson
    manifest.json
  rejected/{import_id}/manifest.json
```

Arquivos rejeitados permanecem na quarentena. Os limites e caminhos são configuráveis pelas variáveis documentadas em `.env.example`; credenciais do PostGIS e do Airflow não devem ser versionadas.

## Estado dos sete cenários de interface

Todos os sete casos de uso de interface do TCC 2 (`UC-CW01` a `UC-CW07`) têm rota, autenticação,
formulário próprio no frontend e evidência de ponta a ponta (API, Airflow e PostGIS reais):
cadastro poligonal e pontual em lote (`UC-CW01`/`UC-CW02`), lote atômico UC+ZA
(`UC-CW03`, `POST /api/v1/import-batches`), atualização versionada com busca por identificador
forte (`UC-CW04`), extinção lógica auditável (`UC-CW05`, `POST /api/v1/ucs/{id}/extinguish`),
substituição de ponto por polígono (`UC-CW06`, `operation=replace_point`) e substituição de Buffer de Abrangência
por ZA oficial (`UC-CW07`). A publicação isolada `za_oficial/create` continua falhando de modo
seguro com `ZA_CREATE_REQUIRES_BATCH` — cadastrar UC e ZA novas juntas exige o lote de `UC-CW03`.
## Documentação

- `docs/GUIA_TESTES_POSTMAN.md`: roteiro manual completo de criação, duplicidade e atualização;
- `docs/DEVELOPMENT_STANDARDS.md`: padrões de código e de documentação dos três repositórios.
