# Guia completo de testes da FastAPI no Postman

## Como copiar os Bodies deste guia

Há dois tipos de requisição:

1. **Upload de arquivo:** use `Body → form-data`. Não use `raw JSON`, porque JSON puro não
   transporta o arquivo. Nas linhas `metadata`, selecione o tipo **Text** e cole integralmente o
   bloco JSON indicado, incluindo `{` e `}`. O Postman aceita o JSON em uma linha ou multilinha.
2. **Comando sem arquivo:** use `Body → raw → JSON` e cole o objeto completo mostrado no guia.

Não adicione manualmente o header `Content-Type` nos uploads. O Postman cria
`multipart/form-data` com o `boundary` correto. Para Bodies `raw`, selecione **JSON** no seletor à
direita do editor; nesse caso o Postman adiciona `Content-Type: application/json`.

## 1. O que este roteiro comprova

Este roteiro testa o ingresso de uma nova Unidade de Conservação sem contornar o pipeline.

O fluxo arquitetural obrigatório é:

```text
Postman
  → FastAPI: upload, quarentena e validação
  → camada Bronze: original + canônico + manifesto
  → Airflow: DAG_UCS
  → PostGIS: UC materializada
  → Airflow: DAG_ZA_BUFFER
  → PostGIS: ZA oficial ou Buffer de Abrangência derivado
  → Airflow: DAGs temáticas afetadas
  → PostGIS: cruzamentos espaciais atualizados
```

Não existe `POST /api/v1/ucs`. Essa coleção é somente leitura porque as UCs consultadas nela
precisam ser produtos da `DAG_UCS`. O comando que promoverá uma importação aceita é
`POST /api/v1/imports/{import_id}/publish`.

`uc/create` aceita uma ou várias features. Cada UC do lote deve possuir nome e pelo menos uma
identidade forte nos atributos. Use `duplicate_policy=reject_batch` para atomicidade estrita ou
`duplicate_policy=skip_duplicates` para publicar somente as inéditas. `uc/update` continua
exigindo exatamente uma feature.

> Estado atual: upload, validação, publicação Bronze e orquestração do Airflow estão habilitados
> para `operation=create`/`update` de UC e `operation=replace_buffer_abrangencia` de ZA oficial. O último POST
> responde `202 Accepted` e inicia o processamento. Isso
> ainda não significa cadastro concluído: acompanhe a importação até `SUCCEEDED`, `DUPLICATE`
> ou `FAILED`.

## 2. Pré-requisitos

- FastAPI em `http://localhost:8000`;
- Postman Desktop;
- para GeoJSON, pode ser usado `tests/fixtures/geospatial/valid_uc.geojson`;
- para KML, pode ser usado `tests/fixtures/geospatial/valid_uc.kml`;
- para Shapefile, um ZIP contendo um único conjunto `.shp`, `.shx`, `.dbf` e `.prj`; sidecars
  seguros do mesmo dataset, como `.cpg` e `.qmd`, são aceitos.

Os arquivos reais atualmente separados para o ensaio são:

- `X:\base_teste\AREA_DE_PROTECAO_AMBIENTAL_DA_REPRESA_ALTO_RIO_PRETO.geojson`;
- `X:\base_teste\PARQUE_NACIONAL_DA_SERRA_DO_ITAJAI.zip`.

Ambos contêm `nome_uc`, `uc_id`, `cd_cnuc` e `wdpa_pid`; por isso não é necessário repetir
`name` no formulário. Em 16/08/2026, uma consulta somente leitura não encontrou esses
identificadores no PostGIS de desenvolvimento.

Confirme primeiro:

```http
GET http://localhost:8000/api/v1/health/ready
```

Resposta esperada: `200 OK`.

## 3. Ambiente do Postman

Crie um Environment chamado `Protected Areas SC - Dev`:

| Variável | Valor inicial |
|---|---|
| `base_url` | `http://localhost:8000` |
| `import_id` | vazio |
| `uc_import_id` | vazio |
| `za_import_id` | vazio |
| `batch_id` | vazio |
| `uc_id` | vazio |
| `idempotency_key` | vazio |
| `correlation_id` | `postman-{{$guid}}` |
| `expected_version` | `1` |
| `username` | valor de `PA_SC_ADMIN_BOOTSTRAP_USERNAME` (seção 3.1) |
| `password` | valor de `PA_SC_ADMIN_BOOTSTRAP_PASSWORD`, marcado como *secret* no Environment |

Crie uma Collection chamada `Protected Areas SC API`. Em **Authorization**, mantenha `No Auth`
— a sessão viaja por cookie, não pelo cabeçalho `Authorization`; o Postman guarda e reenvia esse
cookie sozinho depois do login da seção 3.1, contanto que todas as chamadas usem o mesmo
`base_url` e o Environment não seja trocado no meio do roteiro.
Antes de iniciar uma nova operação, gere uma chave uma única vez no Console do Postman ou use
este script em uma requisição auxiliar:

```javascript
pm.environment.set("idempotency_key", pm.variables.replaceIn("{{$guid}}"));
```

Alternativamente, cole diretamente um UUID no valor atual de `idempotency_key`, por exemplo:

```text
7cb0d867-7bd4-4dd8-b03c-c91679949d1a
```

Não deixe esse script executar automaticamente antes de cada retry, pois ele geraria uma chave
nova. O frontend futuro fará isso internamente: cria a chave no primeiro envio, conserva-a durante
as tentativas da mesma operação e gera outra somente quando o usuário inicia outra operação.

## 3.1. Autenticação (obrigatória desde 27/09/2026)

Toda rota de `/api/v1/imports` e `/api/v1/ucs` exige sessão. Sem isso, qualquer chamada das
seções seguintes devolve `401 NOT_AUTHENTICATED`.

Pré-requisito único: a API precisa ter subido pelo menos uma vez com
`PA_SC_ADMIN_BOOTSTRAP_USERNAME` e `PA_SC_ADMIN_BOOTSTRAP_PASSWORD` definidas, para existir um
administrador. Sem isso, `app_user` fica vazia e nenhum login funciona — a API loga um aviso
claro nesse caso, não falha silenciosamente.

Sem TLS local, defina também `PA_SC_SESSION_COOKIE_SECURE=false` antes de subir a API. O cookie
de sessão é `Secure` por padrão; um navegador real aceita isso em `http://localhost`, mas o
Postman, testando sobre HTTP puro, pode não reenviar um cookie `Secure` do mesmo jeito. Não
precisa disso ao testar através de um navegador real.

Adicione ao Environment: `username` (o valor de `PA_SC_ADMIN_BOOTSTRAP_USERNAME`) e `password`
(o valor de `PA_SC_ADMIN_BOOTSTRAP_PASSWORD`).

```http
POST {{base_url}}/api/v1/auth/login
Content-Type: application/json

{
  "username": "{{username}}",
  "password": "{{password}}"
}
```

Resposta esperada: `200 OK` com `{"id": ..., "nome": "...", "role": "admin"}` e um `Set-Cookie`
para `pa_sc_session` — o Postman guarda esse cookie automaticamente e o reenvia em toda chamada
seguinte para o mesmo `base_url`. Repita este login sempre que o cookie expirar (padrão de
`PA_SC_SESSION_TTL_HOURS`, 12 horas) ou depois de um `POST {{base_url}}/api/v1/auth/logout`.

Para comprovar de propósito a rejeição sem sessão, abra o gerenciador de cookies do Postman,
remova `pa_sc_session` do domínio de `base_url` e repita qualquer chamada da seção 4 em diante:
a resposta deve voltar `401 NOT_AUTHENTICATED`.

## 4. Teste A — verificar a documentação publicada

Requisição:

```http
GET {{base_url}}/openapi.json
```

Em **Scripts → Post-response**, use:

```javascript
pm.test("OpenAPI disponível", () => pm.response.to.have.status(200));
const api = pm.response.json();
pm.expect(api.paths).to.have.property("/api/v1/imports");
pm.expect(api.paths).to.have.property("/api/v1/imports/{import_id}/publish");
pm.expect(api.paths).to.not.have.property("/api/v1/ucs");
pm.expect(api.paths).to.have.property("/api/v1/ucs/{uc_id}");
```

Também confira `{{base_url}}/docs`. O Swagger deve separar:

- **Importações geoespaciais**: comandos de upload, validação e publicação;
- **Unidades de Conservação**: consultas somente leitura do resultado do pipeline.

## 5. Teste B — enviar uma UC com polígono oficial

Crie uma requisição:

```http
POST {{base_url}}/api/v1/imports
```

Você pode selecionar `tests/fixtures/geospatial/valid_uc.geojson`. Se preferir criar um arquivo
próprio, salve o conteúdo abaixo como `uc-poligono-postman.geojson`:

```json
{
  "type": "FeatureCollection",
  "crs": {
    "type": "name",
    "properties": {
      "name": "EPSG:4674"
    }
  },
  "features": [
    {
      "type": "Feature",
      "properties": {
        "nm_uc": "UC poligonal inédita",
        "uc_id": "UC-POSTMAN-POLYGON-001"
      },
      "geometry": {
        "type": "Polygon",
        "coordinates": [
          [
            [-49.20, -27.20],
            [-49.00, -27.20],
            [-49.00, -27.00],
            [-49.20, -27.00],
            [-49.20, -27.20]
          ]
        ]
      }
    }
  ]
}
```

Se essa UC já tiver sido publicada anteriormente, troque `uc_id`, nome, coordenadas e gere outra
`Idempotency-Key`; alterar apenas a chave não transforma uma UC repetida em UC nova.

Headers:

| Chave | Valor |
|---|---|
| `Idempotency-Key` | `{{idempotency_key}}` |
| `X-Correlation-ID` | `{{correlation_id}}` |

Em **Body → form-data**:

| Chave | Tipo | Valor |
|---|---|---|
| `file` | File | selecione `tests/fixtures/geospatial/valid_uc.geojson` |
| `domain` | Text | `uc` |
| `operation` | Text | `create` |
| `metadata` | Text | cole o JSON abaixo |
| `repair_geometry` | Text | `false` |

JSON pronto para colar no valor do campo `metadata`:

```json
{
  "source": "Postman TCC 3",
  "name": "UC poligonal inédita",
  "official_identifier": "UC-POSTMAN-POLYGON-001",
  "source_crs": "EPSG:4674",
  "actor": "operador-postman"
}
```

Resumo visual do form-data — não envie este objeto como `raw JSON`:

```json
{
  "file": "<File: valid_uc.geojson>",
  "domain": "uc",
  "operation": "create",
  "metadata": {
    "source": "Postman TCC 3",
    "name": "UC poligonal inédita",
    "official_identifier": "UC-POSTMAN-POLYGON-001",
    "source_crs": "EPSG:4674",
    "actor": "operador-postman"
  },
  "repair_geometry": false
}
```

Não defina manualmente `Content-Type`: o Postman precisa gerar o boundary de `multipart/form-data`.

Resposta esperada:

- HTTP `202 Accepted`;
- `status` igual a `ACCEPTED`;
- `domain` igual a `uc`;
- `canonical_key` e `manifest_key` preenchidos;
- nenhum registro criado ainda na tabela `uc`.

Script pós-resposta:

```javascript
pm.test("Importação aceita", () => pm.response.to.have.status(202));
const body = pm.response.json();
pm.expect(body.status).to.eql("ACCEPTED");
pm.expect(body.domain).to.eql("uc");
pm.expect(body.canonical_key).to.be.a("string");
pm.expect(body.manifest_key).to.be.a("string");
pm.environment.set("import_id", body.import_id);
```

## 6. Teste C — consultar a importação

```http
GET {{base_url}}/api/v1/imports/{{import_id}}
```

Resposta esperada: `200 OK`, com o mesmo `import_id`, checksum e estado `ACCEPTED`.

```javascript
pm.test("Importação localizada", () => pm.response.to.have.status(200));
pm.expect(pm.response.json().import_id).to.eql(pm.environment.get("import_id"));
```

## 7. Teste D — consultar o relatório geoespacial

```http
GET {{base_url}}/api/v1/imports/{{import_id}}/validation
```

Verifique:

- `valid: true`;
- `target_crs: "EPSG:4674"`;
- `geometry_types` com `Polygon` ou `MultiPolygon`;
- `feature_count` maior que zero;
- `errors` vazio.

```javascript
pm.test("Geometria válida", () => pm.response.to.have.status(200));
const report = pm.response.json();
pm.expect(report.valid).to.eql(true);
pm.expect(report.target_crs).to.eql("EPSG:4674");
pm.expect(report.errors).to.eql([]);
```

## 8. Teste E — solicitar publicação na Bronze e processamento

> Atenção: esta etapa dispara as DAGs reais e pode criar uma UC no PostGIS de desenvolvimento.
> Use nome e identificador deliberados e registre o `import_id`; não publique apenas para testar
> a interface. Os testes A a D não escrevem nas tabelas geoespaciais de domínio.

```http
POST {{base_url}}/api/v1/imports/{{import_id}}/publish
```

Em **Body**, selecione `none`. Essa requisição não recebe JSON nem form-data.

### Resultado esperado

```json
{
  "status": "PROCESSING",
  "dag_id": "DAG_UCS",
  "dag_run_id": "api__<import_id>",
  "bronze_manifest_key": "ucs/import_id=<import_id>/manifest.json"
}
```

O HTTP é `202 Accepted`. A aceitação não significa que a UC já existe no PostGIS. A conclusão só
ocorre após `DAG_UCS`, `DAG_ZA_BUFFER` e as DAGs temáticas necessárias.

```javascript
pm.test("Publicação aceita para processamento", () => {
  pm.response.to.have.status(202);
});
const publication = pm.response.json();
pm.expect(publication.status).to.eql("PROCESSING");
pm.expect(publication.dag_id).to.eql("DAG_UCS");
pm.expect(publication.dag_run_id).to.eql(`api__${pm.environment.get("import_id")}`);
```

### Acompanhamento

Repita `GET /api/v1/imports/{{import_id}}`. Enquanto a cadeia estiver ativa, o estado é
`PROCESSING`; no fim, será `SUCCEEDED`, `DUPLICATE` ou `FAILED`.

## 9. Teste F — comprovar que a escrita direta foi bloqueada

```http
POST {{base_url}}/api/v1/ucs
```

Headers:

| Chave | Valor |
|---|---|
| `Content-Type` | `application/json` |
| `Idempotency-Key` | `tentativa-direta-001` |

Em **Body → raw → JSON**, cole:

```json
{
  "name": "UC que não deve ser inserida diretamente"
}
```

Resposta esperada: `404 Not Found`, pois a rota mutável não existe. Esse teste protege a regra de que nenhuma UC pode
aparecer no PostGIS sem ter ingressado na Bronze e passado pelo Airflow.

## 10. Teste G — consultar uma UC já produzida pelo pipeline

Depois que a integração estiver implementada e a operação concluída, obtenha o `uc_id` do estado
da importação ou do resultado da DAG e grave-o no ambiente. Nesta rota, `uc_id` é o identificador
interno numérico do PostGIS, por exemplo `103`; ele não é o `official_identifier` enviado nos
metadados.

```http
GET {{base_url}}/api/v1/ucs/{{uc_id}}
```

Resposta esperada: `200 OK`. Esta chamada lê o PostGIS; não dispara DAG e não altera dados.

Em **Scripts → Post-response**, você pode guardar a versão vigente:

```javascript
pm.test("UC consultada", () => pm.response.to.have.status(200));
const uc = pm.response.json();
pm.environment.set("expected_version", String(uc.geometry_version));
```

Para uma UC inexistente, espere `404 UC_NOT_FOUND`. Se o DSN não estiver configurado, espere
`503 CADASTRAL_DATABASE_NOT_CONFIGURED`.

## 11. Teste H — consultar Buffers de Abrangência gerados pela DAG

```http
GET {{base_url}}/api/v1/ucs/{{uc_id}}/buffer-abrangencia
```

Esta rota apenas consulta os Buffers de Abrangência materializados pela `DAG_ZA_BUFFER`. A FastAPI não calcula buffer
nem insere `buffer_abrangencia`.

### 11.1. Atualizar uma UC preservando histórico

Primeiro consulte `GET {{base_url}}/api/v1/ucs/{{uc_id}}` e copie `geometry_version` para
`{{expected_version}}`. Gere uma nova `Idempotency-Key` e envie um arquivo contendo exatamente
uma UC completa. Para atualizar a UC criada no Teste B, salve este exemplo como
`uc-poligono-postman-atualizada.geojson`:

```json
{
  "type": "FeatureCollection",
  "crs": {
    "type": "name",
    "properties": {
      "name": "EPSG:4674"
    }
  },
  "features": [
    {
      "type": "Feature",
      "properties": {
        "nm_uc": "UC poligonal inédita — limite atualizado",
        "uc_id": "UC-POSTMAN-POLYGON-001"
      },
      "geometry": {
        "type": "Polygon",
        "coordinates": [
          [
            [-49.21, -27.21],
            [-48.99, -27.21],
            [-48.99, -26.99],
            [-49.21, -26.99],
            [-49.21, -27.21]
          ]
        ]
      }
    }
  ]
}
```

```http
POST {{base_url}}/api/v1/imports
Idempotency-Key: {{idempotency_key}}
X-Correlation-ID: {{correlation_id}}
```

Em **Body → form-data**:

| Chave | Tipo | Valor |
|---|---|---|
| `file` | File | `uc-poligono-postman-atualizada.geojson` |
| `domain` | Text | `uc` |
| `operation` | Text | `update` |
| `metadata` | Text | cole o JSON abaixo |
| `repair_geometry` | Text | `false` |

JSON pronto para colar no valor do campo `metadata`:

```json
{
  "source": "Postman TCC 3",
  "official_identifier": "UC-POSTMAN-POLYGON-001",
  "expected_version": {{expected_version}},
  "reason": "Correção do limite oficial",
  "actor": "operador-postman"
}
```

Resumo visual do form-data — não envie como `raw JSON`:

```json
{
  "file": "<File: geometria completa atualizada>",
  "domain": "uc",
  "operation": "update",
  "metadata": {
    "source": "Postman TCC 3",
    "official_identifier": "UC-POSTMAN-POLYGON-001",
    "expected_version": {{expected_version}},
    "reason": "Correção do limite oficial",
    "actor": "operador-postman"
  },
  "repair_geometry": false
}
```

Após `202 ACCEPTED`, publique em `POST /imports/{{import_id}}/publish` e acompanhe até
`SUCCEEDED`. O resultado correto mantém o mesmo `id_uc`, incrementa `version`, encerra a versão
geométrica e o Buffer de Abrangência anteriores, cria as novas versões e reprocessa PRODES/MapBiomas Alerta.

Se outra operação já tiver alterado a UC, o processamento termina em `FAILED` com
`error_code=UC_VERSION_CONFLICT`. Consulte novamente a UC, revise a alteração concorrente e só
então gere uma nova operação com outra `Idempotency-Key` e a nova versão esperada.

## 12. Cadastrar UC por ponto

Uma UC pontual segue exatamente o mesmo fluxo. Crie um arquivo `uc-ponto.geojson`:

```json
{
  "type": "FeatureCollection",
  "crs": {"type": "name", "properties": {"name": "EPSG:4674"}},
  "features": [
    {
      "type": "Feature",
      "properties": {"name": "UC pontual de demonstração"},
      "geometry": {"type": "Point", "coordinates": [-49.10, -27.10]}
    }
  ]
}
```

Crie uma nova requisição a partir de **A — Criar importação** e use:

| Chave | Tipo | Valor |
|---|---|---|
| `file` | File | `uc-ponto.geojson` |
| `domain` | Text | `uc` |
| `operation` | Text | `create` |
| `metadata` | Text | cole o JSON abaixo |
| `repair_geometry` | Text | `false` |

JSON pronto para colar no valor do campo `metadata`:

```json
{
  "source": "Postman TCC 3",
  "name": "UC pontual inédita",
  "official_identifier": "POINT-POSTMAN-001",
  "source_crs": "EPSG:4674",
  "actor": "operador-postman"
}
```

Resumo visual do form-data — não envie como `raw JSON`:

```json
{
  "file": "<File: uc-ponto.geojson>",
  "domain": "uc",
  "operation": "create",
  "metadata": {
    "source": "Postman TCC 3",
    "name": "UC pontual inédita",
    "official_identifier": "POINT-POSTMAN-001",
    "source_crs": "EPSG:4674",
    "actor": "operador-postman"
  },
  "repair_geometry": false
}
```

Use uma nova `Idempotency-Key`. A resposta deve ficar `ACCEPTED`, e o relatório de validação
deve informar `geometry_types=["Point"]` e `feature_count=1`. Publique e acompanhe até
`SUCCEEDED`.

O lote pontual precisa existir na Bronze. A `DAG_UCS` persiste `Point`; somente a branch de Buffer de Abrangência
usa essa geometria como referência para o buffer métrico de 3.000 m e persiste um
`MultiPolygon`. Não há atalho de escrita direta por latitude/longitude no PostGIS.

Para testar duplicidade, repita upload e publicação do mesmo arquivo com **outra**
`Idempotency-Key`. O upload será validado, mas a publicação deve retornar
`409 UC_ALREADY_EXISTS`, o estado deve ficar `DUPLICATE` e nenhum lote Bronze deve ser criado
para a segunda importação.

## 13. ZA oficial

O upload e a validação de uma ZA oficial usam
`POST {{base_url}}/api/v1/imports` com `Body → form-data`:

Salve o exemplo abaixo como `za-oficial-postman.geojson`:

```json
{
  "type": "FeatureCollection",
  "crs": {
    "type": "name",
    "properties": {
      "name": "EPSG:4674"
    }
  },
  "features": [
    {
      "type": "Feature",
      "properties": {
        "nm_za": "ZA oficial da UC Postman",
        "uc_id": "UC-POSTMAN-POLYGON-001"
      },
      "geometry": {
        "type": "Polygon",
        "coordinates": [
          [
            [-49.25, -27.25],
            [-48.95, -27.25],
            [-48.95, -26.95],
            [-49.25, -26.95],
            [-49.25, -27.25]
          ]
        ]
      }
    }
  ]
}
```

| Chave | Tipo | Valor |
|---|---|---|
| `file` | File | GeoJSON/KML/ZIP com `Polygon` ou `MultiPolygon` |
| `domain` | Text | `za_oficial` |
| `operation` | Text | `replace_buffer_abrangencia` para uma UC existente com Buffer de Abrangência ativo |
| `metadata` | Text | cole o JSON abaixo |
| `repair_geometry` | Text | `false` |

JSON pronto para colar no valor do campo `metadata`:

```json
{
  "source": "Postman TCC 3",
  "uc_identifier": "UC-POSTMAN-POLYGON-001",
  "reason": "Publicação posterior da zona de amortecimento oficial",
  "source_crs": "EPSG:4674",
  "actor": "operador-postman"
}
```

Resumo visual do form-data — não envie como `raw JSON`:

```json
{
  "file": "<File: za-oficial.geojson>",
  "domain": "za_oficial",
  "operation": "replace_buffer_abrangencia",
  "metadata": {
    "source": "Postman TCC 3",
    "uc_identifier": "UC-POSTMAN-POLYGON-001",
    "reason": "Publicação posterior da zona de amortecimento oficial",
    "source_crs": "EPSG:4674",
    "actor": "operador-postman"
  },
  "repair_geometry": false
}
```

Publique a importação aceita com:

```http
POST {{base_url}}/api/v1/imports/{{import_id}}/publish
```

Body: `none`. A resposta inicial esperada é `202 Accepted`. Consulte a importação até
`SUCCEEDED`; então confirme que a ZA está ativa, o Buffer de Abrangência anterior está inativo e os cruzamentos
PRODES/MapBiomas Alerta foram reprocessados. A operação inteira ocorre via Bronze/Airflow; a API
não escreve a ZA no PostGIS.

Existem dois contextos distintos:

- `UC-CW03`: UC e ZA novas são validadas separadamente, agrupadas em `POST /import-batches` e
  publicadas atomicamente;
- `UC-CW07`: já implementado por `replace_buffer_abrangencia`; a ZA chega depois para uma UC existente, encerra
  o Buffer de Abrangência ativo e reprocessa os cruzamentos.

A operação `za_oficial/create` isolada pode ser validada, mas sua publicação retorna
`409 ZA_CREATE_REQUIRES_BATCH`. A ZA não é obrigatória no cadastro da UC. Se apenas a UC estiver disponível, use UC-CW01 ou
UC-CW02; o Buffer de Abrangência será gerado pelo pipeline e poderá ser substituído quando a ZA oficial surgir.

### 13.1 Descoberta automática de ZA já existente na Bronze

Não envie outro arquivo pela API quando a ZA oficial já estiver no acervo Bronze legado. Se a
linha possui `ds_fonte`, ela é autoritativa. Quando a UC correspondente passa a existir, uma
execução agendada da `DAG_ZA_BUFFER` pode associar espacialmente essa ZA, inativar o Buffer de Abrangência e executar
os cruzamentos. Esse é o comportamento esperado observado na UC Alto Rio Preto.

Essa associação espacial é exclusiva do acervo legado autoritativo. Uploads da API usam
`replace_buffer_abrangencia` e exigem `uc_identifier`, evitando vínculos implícitos em operações dirigidas.

### 13.2 Roteiro planejado para UC+ZA simultâneas

O contrato `import-batches` está implementado; execute o roteiro no ambiente Docker:

1. criar e validar uma importação `domain=uc`;
2. criar e validar uma importação `domain=za_oficial` referenciando a mesma UC;
3. enviar `POST /api/v1/import-batches` com os dois `import_id` e nova `Idempotency-Key`;
4. publicar `POST /api/v1/import-batches/{batch_id}/publish`;
5. acompanhar até `SUCCEEDED`;
6. confirmar UC e ZA ativas e ausência de Buffer de Abrangência para a UC;
7. repetir com falha controlada na ZA e confirmar que nenhuma das duas foi persistida.

Após criar a importação da UC, use em **Scripts → Post-response**:

```javascript
pm.test("Importação de UC aceita", () => pm.response.to.have.status(202));
const body = pm.response.json();
pm.expect(body.status).to.eql("ACCEPTED");
pm.environment.set("uc_import_id", body.import_id);
```

Após criar a importação da ZA, use:

```javascript
pm.test("Importação de ZA aceita", () => pm.response.to.have.status(202));
const body = pm.response.json();
pm.expect(body.status).to.eql("ACCEPTED");
pm.environment.set("za_import_id", body.import_id);
```

Body de `POST {{base_url}}/api/v1/import-batches`, em **Body → raw → JSON**:

```json
{
  "uc_import_id": "{{uc_import_id}}",
  "official_zone_import_id": "{{za_import_id}}",
  "reason": "Cadastro conjunto de UC e ZA no mesmo ato oficial",
  "actor": "operador-postman"
}
```

Headers planejados:

| Chave | Valor |
|---|---|
| `Idempotency-Key` | `{{idempotency_key}}` |
| `X-Correlation-ID` | `{{correlation_id}}` |
| `Content-Type` | `application/json` |

Body de `POST /api/v1/import-batches/{{batch_id}}/publish`: selecione `none`.

Quando a criação de batch estiver implementada, salve o identificador com:

```javascript
pm.test("Batch aceito", () => pm.response.to.have.status(202));
const body = pm.response.json();
pm.environment.set("batch_id", body.batch_id);
```

## 14. Idempotência

Repita o mesmo upload com a mesma `Idempotency-Key` e o mesmo conteúdo. A API deve devolver o
mesmo `import_id`. Reutilize a chave alterando arquivo ou metadados: a resposta deve ser
`409 IDEMPOTENCY_KEY_REUSED`.

Repetir `POST /imports/{id}/publish` não cria outro lote lógico nem outra execução concorrente para
a mesma operação.

### 14.1. UC repetida não é o mesmo que retry idempotente

Para simular alguém tentando cadastrar como nova uma UC que já foi concluída:

1. mantenha exatamente o mesmo arquivo;
2. gere uma **nova** `Idempotency-Key`, pois se trata de uma nova intenção de cadastro;
3. execute novamente upload, consulta e publicação;
4. no `POST /imports/{novo_import_id}/publish`, espere `409 UC_ALREADY_EXISTS` quando a API já
   encontrar a UC no PostGIS;
5. consulte `GET /imports/{novo_import_id}` e espere:

```json
{
  "status": "DUPLICATE",
  "error_code": "UC_ALREADY_EXISTS",
  "error_detail": "A UC não foi publicada porque já existe cadastro correspondente no PostGIS.",
  "duplicate_matches": [
    {
      "uc_id": 123,
      "official_identifier": "...",
      "cd_cnuc": "...",
      "wdpa_pid": "...",
      "name": "...",
      "matched_by": "uc_id"
    }
  ]
}
```

A API faz uma consulta preventiva somente leitura antes da Bronze. O Airflow verifica novamente
antes da carga para cobrir concorrência; se ele detectar a duplicidade, grava um resultado
correlacionado e o mesmo GET muda de `PROCESSING` para `DUPLICATE`. O PostGIS mantém as
constraints como última barreira. `name` sozinho não rejeita uma UC; os critérios fortes são
`uc_id`, `cd_cnuc`, `wdpa_pid` e equivalência geométrica.

### 14.2. Lote misto de criação

Inclua o campo multipart `duplicate_policy=skip_duplicates`. Se duas UCs forem inéditas e uma
já existir, a publicação responde `202`, aciona a DAG somente com as duas inéditas e retorna
`batch_items` com estados `ACCEPTED` e `SKIPPED_DUPLICATE`. O original permanece preservado;
o canônico Bronze contém somente as aceitas e o manifesto registra a decisão por feature. Se
todas forem duplicadas, a publicação retorna `409 UC_ALREADY_EXISTS` e não cria lote Bronze.

## 15. Casos de erro recomendados

| Caso | Resultado esperado |
|---|---|
| metadado de UC sem `name`, mas arquivo com `nome_uc` | `202 ACCEPTED`; nome inferido |
| UC sem nome nos metadados e no arquivo | importação `REJECTED` com `MISSING_UC_NAME` |
| nova tentativa de cadastrar UC já existente | `409 UC_ALREADY_EXISTS` no publish e estado `DUPLICATE` |
| arquivo maior que o limite | `413 UPLOAD_TOO_LARGE` |
| extensão incompatível com o conteúdo | `415 UNSUPPORTED_GEOSPATIAL_FORMAT` |
| GeoJSON sem CRS e sem `source_crs` | importação `REJECTED` |
| geometria inválida com `repair_geometry=false` | importação `REJECTED` |
| ZA com geometria Point | importação `REJECTED` |
| `uc/update` com zero ou mais de uma feição | importação `REJECTED`, `UC_OPERATION_REQUIRES_SINGLE_FEATURE` |
| lote misto com `reject_batch` | `409 UC_ALREADY_EXISTS`; nenhuma feição publicada |
| lote misto com `skip_duplicates` | somente inéditas na Bronze/DAG; ignoradas auditadas por feature |
| publicar importação rejeitada | `409 IMPORT_NOT_ACCEPTED` |
| update sem `reason`, versão ou identificador forte | `400 INVALID_UPDATE_METADATA` |
| update com versão esperada desatualizada | estado `FAILED` e `UC_VERSION_CONFLICT` |
| publicar `replace_point` aceito | `202`, processamento assíncrono em `DAG_UCS`; UC deve estar ativa, pontual e na versão esperada |
| publicar `za_oficial/create` isoladamente | `409 ZA_CREATE_REQUIRES_BATCH` |
| `replace_buffer_abrangencia` sem `reason`, vínculo ou com múltiplas feições | rejeição antes da Bronze |
| Bronze ou Airflow indisponível | `503` sem confirmação da mutação de domínio |
| `POST /ucs` | `404 Not Found` (rota inexistente) |

## 16. Evidências para o TCC 3

Preserve capturas ou exports contendo:

1. upload `202` e `import_id`;
2. validação com CRS, tipo e contagens;
3. chaves do original, canônico e manifesto;
4. publicação assíncrona e identificador da execução Airflow, quando implementada;
5. sucesso das DAGs `DAG_UCS` e `DAG_ZA_BUFFER`;
6. execução das DAGs temáticas afetadas;
7. consulta final da UC/Buffer de Abrangência no PostGIS;
8. tentativa de `POST /ucs` bloqueada.
9. segunda tentativa da mesma UC com nova chave, estado `DUPLICATE` e correspondências exibidas.
10. update com mesmo `id_uc`, versões anterior/nova, evento cadastral e nova Buffer de Abrangência/cruzamentos.

Uma evidência de upload aceito, isoladamente, não comprova cadastro concluído. A evidência
end-to-end precisa correlacionar `import_id`, manifesto Bronze, `dag_run_id`, registros no PostGIS
e resultados temáticos.
