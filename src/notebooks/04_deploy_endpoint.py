# Databricks notebook source
# MAGIC %md
# MAGIC # 04 — Deploy do endpoint de Model Serving (`@Champion`)
# MAGIC
# MAGIC - O endpoint recebe só `{"file_path": "/Volumes/..."}`, nunca o JSON de 29 MB.
# MAGIC - O container do Model Serving **não monta `/Volumes`**: o modelo lê o arquivo pela **Files API**,
# MAGIC   autenticado como o service principal `model-serving-volume-reader`. As credenciais entram pelas
# MAGIC   `environment_vars` do endpoint como **referências a secrets**: o valor nunca aparece na config.
# MAGIC - Como o corpo é pequeno (~250 bytes), as **inference tables** do AI Gateway registram todas as chamadas.
# MAGIC - `CAN_QUERY` para o service principal `credit-engine-client` (a Lambda de inferência).
# MAGIC - Idempotente: se o Champion já está servido, não altera a config.

# COMMAND ----------

dbutils.widgets.text("catalog", "")
dbutils.widgets.text("schema", "")
dbutils.widgets.text("model_name", "large_payload_scorer")
dbutils.widgets.text("endpoint_name", "")
dbutils.widgets.text("workload_size", "Small")
dbutils.widgets.dropdown("scale_to_zero", "false", ["true", "false"])
dbutils.widgets.text("databricks_host", "")
dbutils.widgets.text("secret_scope", "mlops-large-payload-modelserving")
dbutils.widgets.text("client_sp_app_id", "")

catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")
model_name = dbutils.widgets.get("model_name")
endpoint_name = dbutils.widgets.get("endpoint_name")
workload_size = dbutils.widgets.get("workload_size")
scale_to_zero = dbutils.widgets.get("scale_to_zero") == "true"
secret_scope = dbutils.widgets.get("secret_scope")
client_sp = dbutils.widgets.get("client_sp_app_id")
full_model_name = f"{catalog}.{schema}.{model_name}"

# COMMAND ----------

import time

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from mlflow.tracking import MlflowClient

w = WorkspaceClient()
host = (dbutils.widgets.get("databricks_host") or w.config.host).rstrip("/")
version = MlflowClient(registry_uri="databricks-uc").get_model_version_by_alias(full_model_name, "Champion").version
served_name = f"{model_name}-{version}"
api = f"/api/2.0/serving-endpoints/{endpoint_name}"
print(f"{full_model_name} v{version} (@Champion) → endpoint {endpoint_name}")


def secret_ref(key: str) -> str:
    # Referência literal, resolvida pelo Model Serving ao subir o container. (Um parâmetro de job
    # com {{secrets/...}} seria resolvido pelo próprio job, por isso a referência é montada aqui.)
    return "{{secrets/" + secret_scope + "/" + key + "}}"


config = {
    "served_entities": [
        {
            "name": served_name,
            "entity_name": full_model_name,
            "entity_version": version,
            "workload_size": workload_size,
            "scale_to_zero_enabled": scale_to_zero,
            # O SDK dentro do modelo lê estas variáveis para autenticar na Files API.
            "environment_vars": {
                "DATABRICKS_HOST": host,
                "DATABRICKS_CLIENT_ID": secret_ref("serving-client-id"),
                "DATABRICKS_CLIENT_SECRET": secret_ref("serving-client-secret"),
            },
        }
    ],
    "traffic_config": {"routes": [{"served_model_name": served_name, "traffic_percentage": 100}]},
}
ai_gateway = {
    "inference_table_config": {
        "enabled": True,
        "catalog_name": catalog,
        "schema_name": schema,
        "table_name_prefix": endpoint_name.replace("-", "_"),
    }
}


def get_endpoint() -> dict:
    return w.api_client.do("GET", api)


def wait_until_ready(timeout_s: float = 40 * 60) -> dict:
    """Numa troca de versão, `ready` pode ficar READY enquanto `config_update` ainda está em andamento."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        ep = get_endpoint()
        state = ep.get("state") or {}
        print(f"ready={state.get('ready')} config_update={state.get('config_update')}")
        if state.get("config_update") == "UPDATE_FAILED":
            raise RuntimeError(f"Falha no deploy de {endpoint_name}: veja os build/event logs do endpoint")
        if state.get("ready") == "READY" and state.get("config_update") in (None, "NOT_UPDATING"):
            return ep
        time.sleep(20)
    raise TimeoutError(f"{endpoint_name} não ficou pronto em {timeout_s / 60:.0f} min")


try:
    current = get_endpoint()
except NotFound:
    current = None

if current is None:
    w.api_client.do("POST", "/api/2.0/serving-endpoints", body={"name": endpoint_name, "config": config, "ai_gateway": ai_gateway})
    print("Endpoint criado")
else:
    current = wait_until_ready()  # um update em andamento faria o PUT falhar
    served = {e.get("entity_version") for e in (current.get("config") or {}).get("served_entities", [])}
    if served == {str(version)}:
        print(f"v{version} já está servida; config inalterada")
    else:
        w.api_client.do("PUT", f"{api}/config", body=config)
        print("Config atualizada")

endpoint = wait_until_ready()

# COMMAND ----------

# Inference tables: em endpoint existente, só podem ser alteradas sem update em andamento.
if current is not None:
    w.api_client.do("PUT", f"{api}/ai-gateway", body=ai_gateway)
# Quem pode chamar: o service principal da Lambda de inferência.
w.api_client.do(
    "PATCH",
    f"/api/2.0/permissions/serving-endpoints/{endpoint['id']}",
    body={"access_control_list": [{"service_principal_name": client_sp, "permission_level": "CAN_QUERY"}]},
)
print(f"Endpoint {endpoint_name} pronto com v{version}; inference tables ativas; CAN_QUERY → {client_sp}")
