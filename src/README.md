# Código do modelo e notebooks

## `model/` — o modelo

| Arquivo | Papel |
|---|---|
| `features.py` | variáveis escalares, features do JSON do birô e leitura do arquivo (POSIX ou Files API) |
| `pyfunc_model.py` | `LargePayloadScorer` (MLflow PyFunc) e a signature, com todas as colunas opcionais |

Esta pasta é registrada **junto com o modelo** (`code_paths`), então o endpoint roda exatamente este
código. Para o modelo real, troque `SCALAR_FEATURES` e `derive_features_from_json` e treine com os
dados reais no notebook 02.

## `notebooks/` — ciclo de vida (executados pelos jobs do bundle)

| Notebook | Job | O que faz |
|---|---|---|
| `01_setup_uc.py` | `setup_uc` | grants no Volume para os dois service principals |
| `02_train_register.py` | `train_register` | treina, registra no Unity Catalog e aponta `@Challenger` |
| `03_evaluate_promote.py` | `evaluate_promote` | gates (métrica, signature, smoke test do `file_path`) e promoção para `@Champion` |
| `04_deploy_endpoint.py` | `deploy_endpoint` | cria/atualiza o endpoint com o Champion, secrets nas env vars, inference tables e `CAN_QUERY` |
| `05_validate_serving.py` | `validate_serving` | `file_path` de 0,01 a 32 MB, SLO, hash divergente e 29 MB inline recusado |

## `common/payloads.py`

Gerador de JSON de birô sintético com tamanho controlado (muitos registros de histórico, como o
documento real), usado nos testes, no notebook 05 e na Lambda simuladora.
