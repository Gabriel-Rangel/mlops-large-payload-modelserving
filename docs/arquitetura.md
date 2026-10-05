# Arquitetura e decisões

## Requisitos

- Modelo de decisão de crédito rodando em shadow mode contra o PEGA até o cutover.
- Request: 15–16 variáveis + histórico completo do birô em JSON, de 15 a 29 MB. O histórico ainda
  não pode ser descartado.
- ~80 requests por dia. A meta é decidir em menos de 1 minuto (hoje a janela é de até 3 min).
- Manter o desenho AWS atual (API Gateway, SQS, Lambdas, SNS) e o Model Serving.

## O que impede 29 MB hoje

| Componente | Limite de corpo | Efeito |
|---|---|---|
| Lambda (invocação síncrona) | 6 MB | API Gateway devolve 413 a partir de ~6 MB |
| API Gateway | 10 MB | — |
| SQS / SNS | 1 MiB / 256 KB | não carregam o payload |
| Model Serving | 16 MB | `Request size cannot exceed 16777216 bytes` |
| Container do Model Serving | sem `/Volumes` montado | `open("/Volumes/...")` falha |

## Claim-check

O payload grande é guardado uma vez no S3 e todos os saltos carregam só a referência. O bucket é
registrado no Unity Catalog como **Volume EXTERNAL**, então a chave S3 e o caminho `/Volumes`
apontam para o mesmo objeto.

1. `POST /uploads` → a Lambda de entrada devolve `request_id` + URL pré-assinada.
2. O Credit Engine faz `PUT` do corpo original (`request.json`, mesmo formato de hoje).
3. `POST /credit-decision {"request_id"}` → a Lambda de entrada confere o arquivo, põe
   `{request_id, s3_key}` na SQS e responde 202.
4. A Lambda de inferência converte a chave em `/Volumes/...` e chama o endpoint com `{"file_path"}`.
5. O modelo lê o arquivo pela **Files API** (uma leitura, com conferência opcional de hash) e pontua.
6. A Lambda grava `response_dbx.json` ao lado do `request.json`, e o bucket dispara o SNS.

Layout no bucket / Volume:

```
payloads/dt=AAAA-MM-DD/<request_id>/request.json        corpo original (15 variáveis + payload)
payloads/dt=AAAA-MM-DD/<request_id>/response_dbx.json   decisão e latência do endpoint
```

## Decisões

| Decisão | Motivo |
|---|---|
| Volume **EXTERNAL** no bucket do cliente | A AWS continua gravando com IAM; o modelo lê o mesmo objeto com governança do Unity Catalog. Nenhuma cópia. |
| Leitura pela **Files API** com service principal | O container não monta `/Volumes`. O acesso fica no Unity Catalog (`READ VOLUME`, auditoria), em vez de IAM direto no S3. |
| Credenciais nas `environment_vars` como **referência a secret** | O valor do secret nunca aparece na configuração do endpoint. |
| Contrato `{"file_path"}` com todas as colunas opcionais | O arquivo pode trazer o request completo; o corpo fica com ~250 bytes. |
| Arquivo lido **uma vez** por request | Hash e features usam os mesmos bytes (29 MB não são baixados duas vezes). |
| Inference tables ligadas | Com o corpo pequeno, todas as chamadas ficam registradas, com o `file_path`. |
| Dependências do modelo fixadas nas versões do treino | O endpoint instala as mesmas versões que serializaram o modelo. |
| Dev sem `mode: development` | Esse modo prefixa o schema com o usuário, e o caminho do Volume faz parte do contrato com a AWS. |

Alternativa considerada: ler o S3 direto com `boto3` e um **instance profile** no endpoint.
Funciona, mas o acesso fica fora do Unity Catalog (governança só por IAM).

## Trade-offs

| Ganho | Custo |
|---|---|
| Mantém API Gateway, SQS, Lambdas e Model Serving | Mais saltos para operar e monitorar |
| Sem limite prático de tamanho (só o S3) | O Credit Engine passa a fazer upload antes do POST |
| Retry e DLQ nativos da SQS | O endpoint precisa ficar sem scale-to-zero em produção (custo fixo) |
| Inference tables nativas | A leitura do arquivo cresce com o tamanho (~1,2 s em 29 MB) |
| Troca de versão do modelo sem downtime | — |

## Segurança

- Credit Engine → API Gateway: a autenticação que já existe hoje (o exemplo usa IAM).
- Lambda de inferência → endpoint: OAuth M2M do service principal `credit-engine-client` (`CAN_QUERY`).
- Endpoint → Volume: service principal `model-serving-volume-reader`, só com `READ VOLUME`.
- Bucket privado, criptografado, com política de retenção; o corpo dos requests não é logado.

## Próximos passos sugeridos

1. Registrar o bucket real como Volume EXTERNAL e apontar o modelo real (`src/model/features.py`).
2. Adaptar as Lambdas atuais conforme `aws/ingress_lambda.py` e `aws/inference_lambda.py`.
3. Endpoint de produção sem scale-to-zero e com alertas de latência e erro sobre as inference tables.
4. Dashboard de comparação PEGA × Databricks para decidir o cutover.
