# Databricks notebook source
# MAGIC %md
# MAGIC # 03 — Gates e promoção (`@Challenger` → `@Champion`)
# MAGIC
# MAGIC Gates antes de promover:
# MAGIC 1. métrica mínima (`min_roc_auc`);
# MAGIC 2. signature registrada;
# MAGIC 3. smoke test do contrato: `{"file_path"}` com o request completo no arquivo tem de dar o
# MAGIC    mesmo score que o contrato legado (variáveis no corpo + arquivo só com o JSON do birô).
# MAGIC
# MAGIC Com `promote_to_champion=true`, aponta o `@Champion`. Depois, rode `deploy_endpoint`.

# COMMAND ----------

dbutils.widgets.text("catalog", "")
dbutils.widgets.text("schema", "")
dbutils.widgets.text("model_name", "large_payload_scorer")
dbutils.widgets.text("payload_volume", "payloads")
dbutils.widgets.text("min_roc_auc", "0.70")
dbutils.widgets.dropdown("promote_to_champion", "false", ["false", "true"])

catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")
model_name = dbutils.widgets.get("model_name")
payload_volume = dbutils.widgets.get("payload_volume")
min_roc_auc = float(dbutils.widgets.get("min_roc_auc"))
promote = dbutils.widgets.get("promote_to_champion") == "true"
full_model_name = f"{catalog}.{schema}.{model_name}"

# COMMAND ----------

import json
from pathlib import Path

import mlflow
import pandas as pd
from mlflow.tracking import MlflowClient

mlflow.set_registry_uri("databricks-uc")
client = MlflowClient(registry_uri="databricks-uc")
version = client.get_model_version_by_alias(full_model_name, "Challenger").version
model_uri = f"models:/{full_model_name}/{version}"
print(f"Avaliando {full_model_name} v{version} (@Challenger)")

# Gate 1 — métrica registrada pelo treino.
roc_auc = float(client.get_model_version(full_model_name, version).tags.get("roc_auc", "0"))
assert roc_auc >= min_roc_auc, f"Gate de métrica: roc_auc {roc_auc} < {min_roc_auc}"

# Gate 2 — signature.
assert mlflow.models.get_model_info(model_uri).signature is not None, "Gate de signature: ausente"

# COMMAND ----------

# Gate 3 — smoke test do contrato. Jobs montam /Volumes via FUSE, então os arquivos são gravados direto.
scalars = {f"var_{i:02d}": 0.1 for i in range(1, 16)}
bureau = {"items": [{"amount": 10.5, "category": "A"}, {"amount": 3.0, "category": "B"}, {"amount": 7.2, "category": "A"}]}
smoke_dir = Path(f"/Volumes/{catalog}/{schema}/{payload_volume}/_smoke")
smoke_dir.mkdir(parents=True, exist_ok=True)
(smoke_dir / "request.json").write_text(json.dumps({**scalars, "payload": bureau}))  # request completo
(smoke_dir / "bureau.json").write_text(json.dumps(bureau))                         # só o JSON do birô

model = mlflow.pyfunc.load_model(model_uri)
minimal = model.predict(pd.DataFrame([{"file_path": str(smoke_dir / "request.json"), "request_id": "smoke"}]))
legacy = model.predict(pd.DataFrame([{**scalars, "payload_uri": str(smoke_dir / "bureau.json"), "request_id": "smoke"}]))
print(minimal, legacy, sep="\n")
assert abs(float(minimal["probability"][0]) - float(legacy["probability"][0])) < 1e-9, "contratos divergem"
print("Gates OK")

# COMMAND ----------

client.set_model_version_tag(full_model_name, version, "validation_status", "valid")
if promote:
    client.set_registered_model_alias(full_model_name, "Champion", version)
    print(f"@Champion → v{version}. Próximo passo: job deploy_endpoint.")
else:
    print(f"Gates OK para v{version}; promote_to_champion=false, Champion inalterado")
