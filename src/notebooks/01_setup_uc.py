# Databricks notebook source
# MAGIC %md
# MAGIC # 01 — Grants do Volume de payloads
# MAGIC
# MAGIC O bundle cria o schema e o Volume `payloads`. Aqui ficam os acessos (GRANT é aditivo):
# MAGIC
# MAGIC | Service principal | Para quê | Acesso |
# MAGIC |---|---|---|
# MAGIC | `model-serving-volume-reader` | o endpoint lê o `request.json` pela Files API | `READ VOLUME` |
# MAGIC | `credit-engine-client` | chama o endpoint; o job de validação grava arquivos de teste | `READ VOLUME`, `WRITE VOLUME` |
# MAGIC
# MAGIC Os dois recebem também `USE CATALOG` e `USE SCHEMA`. Do lado AWS, a Lambda grava no bucket
# MAGIC com IAM (o Volume é EXTERNAL), então não precisa de grant no Unity Catalog para gravar.

# COMMAND ----------

dbutils.widgets.text("catalog", "")
dbutils.widgets.text("schema", "")
dbutils.widgets.text("payload_volume", "payloads")
dbutils.widgets.text("client_sp_app_id", "")
dbutils.widgets.text("serving_sp_app_id", "")

catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")
volume = f"{catalog}.{schema}.{dbutils.widgets.get('payload_volume')}"
grants = {
    dbutils.widgets.get("serving_sp_app_id"): ["READ VOLUME"],
    dbutils.widgets.get("client_sp_app_id"): ["READ VOLUME", "WRITE VOLUME"],
}

for principal, volume_privileges in grants.items():
    statements = [
        f"GRANT USE CATALOG ON CATALOG {catalog} TO `{principal}`",
        f"GRANT USE SCHEMA ON SCHEMA {catalog}.{schema} TO `{principal}`",
        *[f"GRANT {p} ON VOLUME {volume} TO `{principal}`" for p in volume_privileges],
    ]
    for stmt in statements:
        spark.sql(stmt)
        print("OK:", stmt)
