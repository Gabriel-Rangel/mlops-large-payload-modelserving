# Score de payloads grandes com Model Serving (claim-check)

Solução para pontuar o modelo de decisão de crédito com requests de **até ~29 MB**
(15 variáveis + histórico completo do birô em JSON), mantendo o fluxo AWS atual e o Model Serving.

## O problema

O fluxo shadow atual (API Gateway → Lambda → SQS → Lambda → Model Serving) não carrega payloads
grandes. Cada salto tem um limite menor que 29 MB:

| Salto | Limite |
|---|---|
| Lambda (invocação síncrona) | 6 MB |
| API Gateway | 10 MB |
| SQS / SNS | 1 MiB / 256 KB |
| Model Serving | 16 MB |

Medido num ambiente de teste com o mesmo desenho: **5 MB → 202; 9, 15 e 29 MB → 413**.

Além disso, o container do Model Serving **não monta `/Volumes`**: `open("/Volumes/...")` falha
mesmo com permissão.

## A solução

**Claim-check**: o payload entra no S3 uma única vez; API Gateway, SQS e Lambdas carregam só o
ponteiro; o modelo lê o arquivo pela **Files API** do Unity Catalog.

```
Credit Engine ──① PUT 29 MB (URL pré-assinada)──► S3 do cliente = Volume EXTERNAL
Credit Engine ──② POST pequeno {request_id}──► API Gateway → Lambda de entrada → SQS (ponteiro) → 202
Lambda de inferência ──③ {"file_path": "/Volumes/.../request.json"}──► Model Serving
   Model Serving: lê o arquivo pela Files API (service principal, OAuth) → pontua
Lambda de inferência ──④ response_dbx.json──► S3 ──► SNS ──► response router ──► Credit Engine
```

- **Volume EXTERNAL**: o Volume é um prefixo do bucket S3. A chave
  `s3://<bucket>/payloads/.../request.json` e o caminho `/Volumes/<catálogo>/<schema>/payloads/.../request.json`
  apontam para o mesmo arquivo. A AWS grava com IAM e o modelo lê com governança do Unity Catalog.
- **Contrato do endpoint**: `{"file_path": "/Volumes/..."}`, a assinatura que o time já tinha
  pensado. As variáveis podem vir no arquivo (request completo) ou no corpo.
- **Inference tables de volta**: o corpo ao endpoint tem ~250 bytes, então todas as chamadas são registradas.

Detalhes, decisões e trade-offs: [`docs/arquitetura.md`](docs/arquitetura.md).

## Resultados (ambiente de teste, AWS us-east-1, endpoint aquecido)

Do início do upload, pela Lambda, até a decisão chegar pelo SNS:

| Payload | Upload S3 | POST → 202 | Model Serving lê + pontua | Total |
|---|---|---|---|---|
| 15 MB | 0,23 s | 0,19 s | 1,8 s | 4,8 s ¹ |
| 22 MB | 0,33 s | 0,12 s | 1,3 s | 2,9 s |
| **29 MB** | 0,43 s | 0,12 s | **1,2 s** | **2,9 s** |
| 32 MB | 0,44 s | 0,12 s | 1,5 s | 3,5 s |

¹ primeira chamada, com cold start da Lambda. O JSON de teste é sintético, mas realista (~180 mil
registros de histórico em 29 MB). Com scale-to-zero, o endpoint leva minutos para subir: em
produção, deixe desligado.

## Estrutura

```
databricks.yml          bundle: variáveis e ambientes (dev / prod)
resources/              schema + Volume, jobs
src/model/              modelo PyFunc + features (vai junto com o modelo para o Unity Catalog)
src/common/payloads.py  gerador de payload sintético para os testes
src/notebooks/          01 grants · 02 treino · 03 gates/promoção · 04 deploy do endpoint · 05 validação
scripts/                service principals e external location do bucket — ver scripts/README.md
aws/                    Lambdas de referência + exemplo AWS para teste ponta a ponta — ver aws/README.md
tests/                  testes unitários (sem workspace)
docs/arquitetura.md     decisões, limites e trade-offs
```

## Como implantar

Pré-requisitos: Databricks CLI autenticada (`databricks auth login`), um catálogo do Unity Catalog
e permissão para criar storage credential / external location.

**1. Ajuste o `databricks.yml`** no target desejado: `catalog` e `payload_s3_url`. Sem bucket ainda,
use `payload_volume_type: MANAGED` e remova `payload_s3_url`.

**2. Bucket no Unity Catalog** (para o Volume EXTERNAL):

```bash
python scripts/setup_uc_external_volume.py --profile <PERFIL> --aws-profile <PERFIL_AWS> \
  --bucket <bucket> --name <nome_uc> --role-name <role-para-o-unity-catalog>
```

**3. Service principals** (quem chama o endpoint e quem o endpoint usa para ler o Volume):

```bash
python scripts/setup_identities.py --profile <PERFIL> [--aws-profile <PERFIL_AWS>]
```

**4. Bundle, modelo e endpoint:**

```bash
databricks bundle deploy -t dev -p <PERFIL>
databricks bundle run setup_uc -t dev -p <PERFIL>
databricks bundle run train_register -t dev -p <PERFIL>
databricks bundle run evaluate_promote -t dev -p <PERFIL> --params promote_to_champion=true
databricks bundle run deploy_endpoint -t dev -p <PERFIL>
```

**5. Validação:**

```bash
databricks bundle run validate_serving -t dev -p <PERFIL>     # file_path de 0,01 a 32 MB, hash, 29 MB inline
aws/deploy.sh /Volumes/<catálogo>/<schema>/payloads             # exemplo AWS (opcional)
aws/run_e2e.sh inline 5 9 15 29                                 # fluxo de hoje: acima de ~6 MB → 413
aws/run_e2e.sh claim_check 15 22 29 32                          # a proposta, ponta a ponta
```

**Testes unitários** (sem workspace): `python -m unittest discover -s tests -t .`
(precisa de `pandas`, `mlflow`, `scikit-learn`, `boto3`).

## Como o modelo lê o arquivo dentro do Model Serving

```python
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()  # lê DATABRICKS_HOST / DATABRICKS_CLIENT_ID / DATABRICKS_CLIENT_SECRET do ambiente
raw = w.files.download("/Volumes/<catálogo>/<schema>/payloads/.../request.json").contents.read()
```

As três variáveis entram nas `environment_vars` do endpoint como referências a secrets
(`{{secrets/<scope>/serving-client-id}}`); o service principal tem só `READ VOLUME`. Veja
`src/model/features.py` (`read_payload_bytes`) e `src/notebooks/04_deploy_endpoint.py`.

## O que muda para o Credit Engine

1. Antes do POST, envia o corpo original para o S3, por URL pré-assinada (`POST /uploads`) ou
   `PutObject` direto, se tiver IAM no bucket.
2. O POST ao API Gateway leva só o `request_id`. A resposta assíncrona (202) é a mesma de hoje.

## Operação

- **Novo modelo**: `evaluate_promote` e depois `deploy_endpoint` (troca de versão sem downtime).
- **Cold start**: em produção, `enable_scale_to_zero: "false"`.
- **Retries**: a SQS repete 429/5xx (até 3 vezes, depois DLQ); 4xx gera `response_dbx.json` com `failed`.
- **Observabilidade**: inference tables `<schema>.<endpoint>_payload` (request com o `file_path` e
  resposta), e o `response_dbx.json` com as latências.
- **Retenção**: o exemplo AWS expira os payloads em 30 dias; defina a política real com risco e compliance.
