# Scripts de setup

Rodam uma vez por ambiente, a partir de uma máquina com a Databricks CLI (e a AWS CLI, para o bucket).
São idempotentes: rodar de novo não duplica nada.

| Script | O que faz |
|---|---|
| `setup_uc_external_volume.py` | Cria a IAM role do Unity Catalog, a storage credential e a external location no bucket. Pré-requisito do Volume EXTERNAL. |
| `setup_identities.py` | Cria os service principals `credit-engine-client` (chama o endpoint) e `model-serving-volume-reader` (o endpoint lê o Volume), com `workspace-access`, e guarda os OAuth secrets no secret scope e, se pedido, no AWS Secrets Manager. |

```bash
python scripts/setup_uc_external_volume.py --profile <PERFIL> --aws-profile <PERFIL_AWS> \
  --bucket <bucket> --name <nome_uc> --role-name <role>
python scripts/setup_identities.py --profile <PERFIL> [--aws-profile <PERFIL_AWS>]
```

Notas:

- A AWS pode levar ~10 minutos para que o Unity Catalog consiga assumir uma role recém-criada; o
  script espera e tenta de novo.
- Nenhum valor de secret é impresso. Para trocar os secrets, use `--rotate`.
- Os grants no Volume ficam no job `setup_uc` (notebook `01_setup_uc.py`).
