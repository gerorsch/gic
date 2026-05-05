# GIC — Docentes UFRPE e Produção Científica

Pipeline que cruza dados de docentes da pós-graduação UFRPE (CAPES) com
métricas do Scopus (Elsevier) e gera uma ontologia OML para análise da
relação entre tempo de carreira e impacto científico.

Disciplina **Gestão da Informação e do Conhecimento** — UFRPE.

## Estrutura do repositório

```
gic/
├── data/
│   ├── raw/
│   │   ├── capes_docentes/      # CSVs CAPES "Docentes da Pós-Graduação" 2017-2024
│   │   └── scopus/              # CSV Scopus (saída da Fase 2 do pipeline)
│   ├── processed/               # docentes_ufrpe.csv + docentes_ufrpe_vinculos.csv
│   └── fuseki/                  # Volume persistente do Fuseki (criado no 1º run)
├── docker/
│   └── docker-compose.yml       # Apache Jena Fuseki (Sprint 3)
├── scripts/
│   ├── generate_oml_from_capes_docentes_ufrpe.py    # Fase 1: CAPES -> OML
│   ├── eda_docentes_ufrpe.ipynb                     # Análise exploratória
│   └── api-scopus/                                  # Fase 2-4: coleta + validação Scopus
│       ├── gic_main.py            # Busca inicial por nome
│       ├── enrich_metrics.py      # Enriquece com citation/h-index
│       ├── recover_not_found.py   # Recupera not_found com queries alternativas
│       ├── auto_triage.py         # Triagem automática score>=100 / aff!=UFRPE
│       └── review_matches.py      # Revisão manual interativa
├── src/oml/gic.ufrpe.br/cti/    # Ontologia OML
│   ├── vocabulary/cti.oml         # Classes e propriedades (Docente, PPG, ...)
│   ├── description/docentes-ufrpe.oml   # Instâncias geradas pelo script
│   └── bundle/cti.oml             # Bundle (vocabulary + description)
└── Rosetta/gic/                 # Workspace Gradle (build.gradle, catalog.xml, src/sparql/)
```

## Pré-requisitos

- Python 3.10+
- Pacotes: `pandas`, `requests`, `python-dotenv`
- Elsevier API key (cadastro em https://dev.elsevier.com/)
  - **Importante**: a chave pessoal pode não ter acesso a todas as views.
    Se aparecer `401 AUTHORIZATION_ERROR`, é necessário um `INSTTOKEN`
    institucional (solicitar à biblioteca da UFRPE).

```bash
cd scripts/api-scopus
python -m venv .venv
.venv/bin/pip install pandas requests python-dotenv
echo "ELSEVIER_API_KEY=SUA_CHAVE" > .env
# opcional, se a chave pessoal não bastar:
echo "ELSEVIER_INSTTOKEN=SEU_TOKEN" >> .env
```

## Pipeline de reprodução

### Fase 1 — CAPES → docentes filtrados + OML inicial

Filtra os CSVs anuais da CAPES, deixa apenas docentes UFRPE com doutorado,
e gera um OML inicial (sem métricas Scopus).

```bash
scripts/api-scopus/.venv/bin/python scripts/generate_oml_from_capes_docentes_ufrpe.py
```

**Saída:**
- `data/processed/docentes_ufrpe.csv` — docentes UFRPE únicos (~826)
- `src/oml/gic.ufrpe.br/cti/description/docentes-ufrpe.oml` — ontologia

> O script é idempotente: pode rodar quantas vezes quiser, sempre gera o
> mesmo CSV/OML a partir das mesmas entradas.

### Fase 2 — coleta Scopus

Sequência de scripts em `scripts/api-scopus/`. Cada um grava checkpoints
incrementais; interromper no meio não perde progresso.

```bash
cd scripts/api-scopus

# 2.1 — Busca inicial por nome (~826 chamadas Author Search)
.venv/bin/python gic_main.py --skip-metrics --overwrite

# 2.2 — Enriquece com métricas (~680 chamadas Author Retrieval)
.venv/bin/python enrich_metrics.py

# 2.3 — Tenta recuperar not_found com variantes de query
.venv/bin/python recover_not_found.py \
  --input ../../data/raw/scopus/scopus_ufrpe_enriched.csv

# 2.4 — Métricas dos recuperados, gravando no arquivo final
.venv/bin/python enrich_metrics.py \
  --input ../../data/raw/scopus/scopus_ufrpe_recovered.csv \
  --output ../../data/raw/scopus/scopus_ufrpe.csv \
  --only-missing
```

**Saída:** `data/raw/scopus/scopus_ufrpe.csv` com colunas:
`author_name, citation_count, h_index, document_count, scopus_author_id,
scopus_indexed_name, scopus_affiliation_name, match_status, match_score,
query_used, error_message`.

Limites da API pessoal Elsevier: **5000/semana por endpoint** (Search e
Retrieval contam separadamente).

### Fase 3 — validação dos matches

#### 3.1 Triagem automática

Decide os casos óbvios sem intervenção humana, com base na fórmula de score
(em `gic_main.py:108-135`, máximo 155 pontos):

- **`accepted`** — `match_score >= 100`. O teto sem o bônus de afiliação
  UFRPE (+60) é 95, então score ≥ 100 garante afiliação UFRPE algoritmicamente.
- **`rejected`** — score < 100 **e** afiliação Scopus não menciona
  "PERNAMBUCO"/"UFRPE".
- **(em branco)** — restante; entra na revisão manual.

```bash
cd scripts/api-scopus
.venv/bin/python auto_triage.py
```

**Saída:** `data/raw/scopus/scopus_ufrpe_reviewed.csv` (mesmo schema
+ coluna `review_status`).

Distribuição esperada (dados atuais):
- 632 auto-aceitos (score ≥ 100)
- 64 auto-rejeitados (afiliação não-UFRPE)
- 118 pendentes para revisão manual
- 12 `not_found` intactos

#### 3.2 Revisão manual

Apresenta cada match pendente, abre o perfil Scopus no navegador e
pergunta accept/reject. Pode parar e retomar a qualquer momento (`--resume`).

```bash
cd scripts/api-scopus
.venv/bin/python review_matches.py --min-score 0 --max-score 99 --resume
```

Comandos durante a revisão:
- `a` aceitar · `r` rejeitar · `s` pular · `o` reabrir navegador
- `b` desfazer última · `q` salvar e sair · `?` ajuda

Salva a cada 10 decisões (configurável via `--flush-every`).

### Fase 4 — OML final

Após a validação, basta rodar a Fase 1 novamente: o script lê
`data/raw/scopus/scopus_ufrpe.csv` e enriquece o OML com `citation_count`,
`h_index` e `document_count`.

```bash
scripts/api-scopus/.venv/bin/python scripts/generate_oml_from_capes_docentes_ufrpe.py
```

> Para considerar apenas matches aceitos, filtre antes:
> ```bash
> awk -F',' 'NR==1 || $12=="accepted"' data/raw/scopus/scopus_ufrpe_reviewed.csv \
>   > data/raw/scopus/scopus_ufrpe.csv
> ```

## Análise exploratória

Dois notebooks Jupyter em `scripts/`:

- `eda_docentes_ufrpe.ipynb` — análise dos dados CAPES (filtragem, qualidade, tempo de carreira)
- `eda_scopus_ufrpe.ipynb` — análise das métricas Scopus (matching, distribuições, top docentes)

Para rodá-los localmente, instale também as dependências de visualização:

```bash
cd scripts/api-scopus
uv pip install matplotlib seaborn jupyter
.venv/bin/jupyter notebook ../eda_scopus_ufrpe.ipynb
```

## Sprint 3 — Extrair o conhecimento (SPARQL + Fuseki)

A Sprint 3 transforma o OML em um grafo RDF consultável: build com Gradle,
carga em Apache Jena Fuseki (local ou Docker) e consultas SPARQL salvas em
`Rosetta/gic/src/sparql/`.

### Pré-requisitos adicionais

```bash
sudo apt install default-jdk    # Java 11+ para o Gradle
# Docker (opcional, para Fuseki persistente):
# https://docs.docker.com/engine/install/debian/
```

### Fluxo completo (Fuseki embutido no Gradle)

```bash
cd Rosetta/gic
./gradlew check          # OML -> OWL/TTL + reasoning Openllet
./gradlew startFuseki    # sobe Fuseki em-memória local
./gradlew owlLoad        # carrega o grafo
./gradlew owlQuery       # roda src/sparql/*.sparql -> build/results/*.json
./gradlew stopFuseki
```

Saídas:
- `build/owl/gic.ufrpe.br/cti/...` — grafo RDF/OWL gerado
- `build/reports/reasoning.xml` — relatório do reasoner (validação)
- `build/results/*.json` — uma resposta por consulta SPARQL

### Fluxo com Fuseki persistente (Docker)

Para preservar o dataset entre runs e expor um endpoint HTTP API-like:

```bash
# 1. Subir Fuseki em background
cd docker
docker compose up -d

# 2. Gerar OWL (uma vez ou após mudar o OML)
cd ../Rosetta/gic
./gradlew omlToOwl

# 3. Criar dataset 'gic' e carregar todos os RDFs (script idempotente)
cd ../../docker
./load_to_fuseki.sh    # cria dataset, limpa e popula via curl

# 4. Consultar via interface (http://localhost:3030) ou via HTTP:
curl --data-urlencode "query@../Rosetta/gic/src/sparql/1-carreira-vs-impacto.sparql" \
     -H "Accept: application/sparql-results+json" \
     http://localhost:3030/gic/sparql
```

### Consultas SPARQL

| # | Arquivo | Pergunta |
|---|---|---|
| 1 | `1-carreira-vs-impacto.sparql` | **Principal.** Tempo de carreira (anos desde doutorado) × impacto (citações, h, documentos) por docente. |
| 2 | `2-top20-h-index.sparql` | Top 20 docentes por índice h, com seus PPGs. |
| 3 | `3-impacto-por-area.sparql` | Métricas Scopus médias agregadas por área de conhecimento (via PPG). |
| 4 | `4-permanentes-vs-colaboradores.sparql` | Comparação entre as duas categorias CAPES. |
| 5 | `5-bonus-interdisciplinaridade.sparql` | Bônus — docentes que atuam em múltiplos PPGs e seu impacto. |

### Modelo OML

- `cti:Docente` — pessoa identificada por `ID_PESSOA` da CAPES.
  Propriedades: `nm_docente`, `an_titulacao`, `ds_categoria`,
  `citation_count`, `h_index`, `document_count`.
- `cti:PPG` — programa de pós-graduação. Propriedades: `cd_programa_ies`,
  `nm_programa_ies`, `nm_area_conhecimento`.
- Relação `cti:vinculado_a` (Docente → PPG, N:N) — um docente pode estar
  vinculado a múltiplos PPGs e cada PPG tem N docentes.

> A área de conhecimento e o nome do programa **não** são propriedades do
> Docente, e sim do PPG. Para obter as áreas de um docente, navegue pela
> relação `cti:vinculado_a` (ver consultas 3 e 5).

## Documentação adicional

- `scripts/api-scopus/README.md` — detalhes do pipeline Scopus
- `Sprint #2 - Codificar o Conhecimento.pdf` — escopo do sprint anterior
- `Sprint #3 - Extrair o Conhecimento - Consultas SparQL e Fuseki.pptx` — escopo da Sprint 3
- `Docente-e-Producao-Cientifica.pptx.pdf` — apresentação do projeto
