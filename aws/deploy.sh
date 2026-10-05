#!/usr/bin/env bash
# Implanta o exemplo AWS (CloudFormation): bucket, API Gateway, Lambda de entrada, SQS, Lambda de
# inferência, perna de resposta S3 → SNS → inbox e a Lambda que simula o Credit Engine.
#
#   aws/deploy.sh /Volumes/<catálogo>/<schema>/payloads
#
# Variáveis: AWS_PROFILE, AWS_REGION (padrão us-east-1), PREFIX (padrão lp-serving),
#            ENDPOINT_NAME (padrão large-payload-scoring-dev).
set -euo pipefail

VOLUME_ROOT=${1:?uso: aws/deploy.sh /Volumes/<catálogo>/<schema>/payloads}
export AWS_REGION=${AWS_REGION:-us-east-1}
PREFIX=${PREFIX:-lp-serving}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
BUILD="$ROOT/build/lambda"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
ARTIFACTS="$PREFIX-cfn-artifacts-$ACCOUNT"

# Bucket privado para o código empacotado das Lambdas.
if ! aws s3api head-bucket --bucket "$ARTIFACTS" 2>/dev/null; then
  aws s3api create-bucket --bucket "$ARTIFACTS" >/dev/null
  aws s3api put-public-access-block --bucket "$ARTIFACTS" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
fi

# Pacotes das Lambdas: só biblioteca padrão + boto3 (já presente no runtime), sem layer.
package() {  # package <nome> <arquivos...>
  local name=$1; shift
  rm -rf "${BUILD:?}/$name" && mkdir -p "$BUILD/$name" && cp "$@" "$BUILD/$name/"
}
package ingress "$ROOT/aws/ingress_lambda.py"
package inference "$ROOT/aws/inference_lambda.py" "$ROOT/aws/dbx_http.py"
package credit_engine_sim "$ROOT/aws/credit_engine_sim.py" "$ROOT/aws/dbx_http.py" "$ROOT/src/common/payloads.py"

aws cloudformation package --template-file "$ROOT/aws/template.yaml" --s3-bucket "$ARTIFACTS" \
  --s3-prefix "$PREFIX" --output-template-file "$BUILD/template.packaged.yaml" >/dev/null
aws cloudformation deploy --template-file "$BUILD/template.packaged.yaml" --stack-name "$PREFIX" \
  --capabilities CAPABILITY_NAMED_IAM --no-fail-on-empty-changeset --tags project=mlops-large-payload-modelserving \
  --parameter-overrides "Prefix=$PREFIX" "VolumeRoot=$VOLUME_ROOT" "EndpointName=${ENDPOINT_NAME:-large-payload-scoring-dev}"
aws cloudformation describe-stacks --stack-name "$PREFIX" \
  --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output table
