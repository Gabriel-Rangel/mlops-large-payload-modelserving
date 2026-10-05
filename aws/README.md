# Exemplo AWS

Réplica do fluxo shadow com **claim-check**: o payload de 29 MB vai para o S3 uma vez e só o
ponteiro passa pelo API Gateway, pela SQS e pelas Lambdas.

```
Credit Engine ──① POST /uploads──► API Gateway → Lambda de entrada ──► URL pré-assinada
Credit Engine ──② PUT 29 MB──► s3://<bucket>/payloads/dt=.../<id>/request.json
Credit Engine ──③ POST /credit-decision {request_id}──► Lambda de entrada ──► SQS (ponteiro) ──► 202
SQS ──► Lambda de inferência ──④ {"file_path": "/Volumes/.../request.json"}──► Model Serving
Lambda de inferência ──⑤ response_dbx.json──► bucket ──► SNS ──► inbox (no lugar do response router)
```

| Arquivo | Papel |
|---|---|
| `template.yaml` | CloudFormation com todos os recursos acima (parâmetro `Prefix`) |
| `ingress_lambda.py` | `POST /uploads` (URL pré-assinada) e `POST /credit-decision` (202 + ponteiro na SQS) |
| `inference_lambda.py` | SQS → Model Serving com `file_path` → `response_dbx.json`; retry em 429/5xx, DLQ após 3 |
| `credit_engine_sim.py` | simula o Credit Engine e mede o tempo até a decisão chegar na inbox |
| `dbx_http.py` | HTTP + token OAuth da Databricks com biblioteca padrão (sem layer) |
| `deploy.sh` / `run_e2e.sh` / `teardown.sh` | implantar, testar e remover |

## Em produção

As Lambdas de entrada e de inferência deste exemplo mostram as **mudanças** nas Lambdas que o
cliente já tem:

- **Lambda de entrada**: em vez de receber o corpo inteiro (limite de 6 MB), entrega uma URL
  pré-assinada e, no POST, confere que o `request.json` existe e põe só o ponteiro na fila.
  Se o Credit Engine já tiver IAM no bucket, ele pode fazer o `PutObject` direto, sem `/uploads`.
- **Lambda de inferência**: chama o endpoint com `{"file_path": ...}` em vez do payload, e grava
  `response_dbx.json` no bucket (que dispara o SNS de hoje).
- **Bucket**: o mesmo que já guarda `request.json`, registrado como Volume EXTERNAL
  (`scripts/setup_uc_external_volume.py`).

## Uso

```bash
export AWS_PROFILE=<perfil>
aws/deploy.sh /Volumes/<catálogo>/<schema>/payloads   # stack "lp-serving" (mude com PREFIX=...)
aws/run_e2e.sh inline 5 9 15 29                        # o fluxo de hoje: acima de ~6 MB → 413
aws/run_e2e.sh claim_check 15 22 29 32                 # a proposta, com o endpoint no ar
aws/teardown.sh                                        # remove tudo (pede confirmação)
```

O API Gateway usa autenticação IAM (SigV4). A Lambda simuladora tem permissão
`execute-api:Invoke`; o Credit Engine real usa a autenticação que já tem hoje.
