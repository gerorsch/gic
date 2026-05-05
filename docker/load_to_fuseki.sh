#!/usr/bin/env bash
#
# Carrega o grafo RDF gerado em Rosetta/gic/build/owl/ no Fuseki rodando via
# Docker Compose. Útil para reproduzir o ambiente sem cliques na UI.
#
# Uso:
#   docker compose up -d
#   ./load_to_fuseki.sh
#
# Requisitos: curl, jq (apt install jq), Fuseki acessível em localhost:3030
# com credenciais admin/admin (default do docker-compose.yml).

set -euo pipefail

FUSEKI="http://localhost:3030"
AUTH="admin:admin"
DATASET="gic"
OWL_DIR="$(cd "$(dirname "$0")"/../Rosetta/gic/build/owl && pwd)"

if [[ ! -d "$OWL_DIR" ]]; then
  echo "ERRO: $OWL_DIR não existe. Rode antes: cd Rosetta/gic && ./gradlew omlToOwl" >&2
  exit 1
fi

echo "→ Aguardando Fuseki em $FUSEKI..."
for _ in {1..30}; do
  if curl -fsS "$FUSEKI/$/ping" >/dev/null 2>&1; then
    echo "  Fuseki pronto."
    break
  fi
  sleep 1
done

# Cria o dataset se não existir
if curl -fsS -u "$AUTH" "$FUSEKI/$/datasets/$DATASET" >/dev/null 2>&1; then
  echo "→ Dataset '$DATASET' já existe."
else
  echo "→ Criando dataset '$DATASET' (tipo: TDB2 persistente)..."
  curl -fsS -u "$AUTH" -X POST \
    --data "dbName=$DATASET&dbType=tdb2" \
    "$FUSEKI/$/datasets" >/dev/null
fi

# Limpa dados antigos do default graph para idempotência
echo "→ Limpando dataset..."
curl -fsS -u "$AUTH" -X POST \
  --data "update=CLEAR DEFAULT" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  "$FUSEKI/$DATASET/update" >/dev/null

# Carrega todos os .ttl e .owl
shopt -s globstar nullglob
total=0
for f in "$OWL_DIR"/**/*.ttl "$OWL_DIR"/**/*.owl; do
  [[ -f "$f" ]] || continue
  ext="${f##*.}"
  case "$ext" in
    ttl) ctype="text/turtle" ;;
    owl) ctype="application/rdf+xml" ;;
  esac
  echo "  + $(basename "$f")"
  curl -fsS -u "$AUTH" \
    -H "Content-Type: $ctype" \
    --data-binary "@$f" \
    "$FUSEKI/$DATASET/data?default" >/dev/null
  ((total++))
done

echo "→ $total arquivos carregados no dataset '$DATASET'."
echo "→ Endpoint SPARQL: $FUSEKI/$DATASET/sparql"
echo "→ UI:             $FUSEKI/dataset.html?tab=query&ds=/$DATASET"
