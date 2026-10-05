"""Cria os service principals e guarda os OAuth secrets (idempotente).

  credit-engine-client          chama o endpoint (Lambda de inferência e job de validação).
                                Secret → secret scope (`client-id` / `client-secret`) e, opcionalmente,
                                AWS Secrets Manager (lido pelas Lambdas).
  model-serving-volume-reader   identidade com que o endpoint lê o Volume pela Files API.
                                Secret → secret scope (`serving-client-id` / `serving-client-secret`),
                                referenciado nas environment_vars do endpoint.

Os dois recebem o entitlement `workspace-access`. Nenhum valor de secret é impresso. Rodar de novo
mantém os secrets existentes, exceto com --rotate.

Uso:
  python scripts/setup_identities.py --profile <PERFIL_DATABRICKS> \
      [--aws-profile <PERFIL_AWS> --aws-secret-name mlops-large-payload-modelserving/databricks-client]
"""

from __future__ import annotations

import argparse
import json

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.iam import Patch, PatchOp, PatchSchema

# nome do service principal → prefixo das chaves no secret scope
IDENTITIES = {"credit-engine-client": "client", "model-serving-volume-reader": "serving-client"}


def ensure_service_principal(w: WorkspaceClient, name: str):
    found = list(w.service_principals.list(filter=f'displayName eq "{name}"'))
    sp = found[0] if found else w.service_principals.create(display_name=name, active=True)
    if not found:
        print(f"Service principal criado: {name}")
    if "workspace-access" not in {e.value for e in (sp.entitlements or [])}:
        w.service_principals.patch(
            sp.id,
            operations=[Patch(op=PatchOp.ADD, path="entitlements", value=[{"value": "workspace-access"}])],
            schemas=[PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP],
        )
        print(f"  entitlement workspace-access adicionado a {name}")
    return sp


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", required=True, help="perfil da Databricks CLI")
    parser.add_argument("--scope", default="mlops-large-payload-modelserving", help="secret scope do Databricks")
    parser.add_argument("--aws-profile", help="perfil da AWS CLI (omita para não gravar no Secrets Manager)")
    parser.add_argument("--aws-region", default="us-east-1")
    parser.add_argument("--aws-secret-name", default="mlops-large-payload-modelserving/databricks-client")
    parser.add_argument("--rotate", action="store_true", help="gera novos OAuth secrets mesmo se já existirem")
    args = parser.parse_args()

    w = WorkspaceClient(profile=args.profile)
    if args.scope not in {s.name for s in w.secrets.list_scopes()}:
        w.secrets.create_scope(scope=args.scope)
        print(f"Secret scope criado: {args.scope}")
    stored = {s.key for s in w.secrets.list_secrets(scope=args.scope)}

    sm = None
    aws_missing = False
    if args.aws_profile:
        import boto3

        sm = boto3.Session(profile_name=args.aws_profile, region_name=args.aws_region).client("secretsmanager")
        try:
            sm.describe_secret(SecretId=args.aws_secret_name)
        except sm.exceptions.ResourceNotFoundException:
            aws_missing = True

    for name, prefix in IDENTITIES.items():
        sp = ensure_service_principal(w, name)
        print(f"{name}: application_id={sp.application_id}")
        id_key, secret_key = f"{prefix}-id", f"{prefix}-secret"
        # O secret do cliente precisa ser gerado de novo se ainda não estiver no Secrets Manager.
        needs_aws = name == "credit-engine-client" and aws_missing
        if not args.rotate and not needs_aws and {id_key, secret_key} <= stored:
            print(f"  OAuth secret já guardado em {args.scope} (use --rotate para trocar)")
            continue
        # Um service principal pode ter vários secrets; os anteriores continuam válidos até expirarem.
        secret = w.service_principal_secrets_proxy.create(service_principal_id=sp.id)
        w.secrets.put_secret(scope=args.scope, key=id_key, string_value=sp.application_id)
        w.secrets.put_secret(scope=args.scope, key=secret_key, string_value=secret.secret)
        print(f"  OAuth secret guardado em {args.scope}/{secret_key} (expira em {secret.expire_time})")

        if name == "credit-engine-client" and sm is not None:
            value = json.dumps({"client_id": sp.application_id, "client_secret": secret.secret, "host": w.config.host})
            if aws_missing:
                sm.create_secret(Name=args.aws_secret_name, SecretString=value,
                                 Description=f"OAuth M2M do service principal Databricks {name}")
                print(f"  AWS secret criado: {args.aws_secret_name}")
            else:
                sm.put_secret_value(SecretId=args.aws_secret_name, SecretString=value)
                print(f"  AWS secret atualizado: {args.aws_secret_name}")


if __name__ == "__main__":
    main()
