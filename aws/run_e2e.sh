#!/usr/bin/env bash
# Teste ponta a ponta a partir da AWS com a Lambda simuladora. Resultados em build/e2e_results.jsonl.
#
#   aws/run_e2e.sh claim_check 15 22 29 32    a proposta: upload no S3 + POST pequeno
#   aws/run_e2e.sh inline 5 9 15 29           o fluxo de hoje: corpo inteiro no API Gateway (espera 413)
#
# Variáveis: AWS_PROFILE, PREFIX (padrão lp-serving).
set -euo pipefail

MODE=${1:?uso: aws/run_e2e.sh claim_check|inline <tamanhos em MB...>}; shift
export AWS_REGION=${AWS_REGION:-us-east-1}
PREFIX=${PREFIX:-lp-serving}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT="$ROOT/build/e2e_results.jsonl"
mkdir -p "$ROOT/build"

for size in "${@:-29}"; do
  aws lambda invoke --function-name "$PREFIX-credit-engine-sim" --cli-binary-format raw-in-base64-out \
    --cli-read-timeout 330 --payload "{\"mode\": \"$MODE\", \"payload_mb\": $size}" "$ROOT/build/e2e_last.json" >/dev/null
  cat "$ROOT/build/e2e_last.json" >> "$OUT" && echo >> "$OUT"
  python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); print({k: r.get(k) for k in ("mode","payload_mb","ok","http","upload_ms","time_to_202_ms","serving_invoke_ms","e2e_ms","prediction")})' "$ROOT/build/e2e_last.json"
done
