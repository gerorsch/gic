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
│   └── processed/               # docentes_ufrpe.csv (filtrado UFRPE + métricas)
├── scripts/
│   ├── generate_oml_from_capes_docentes_ufrpe.py    # Fase 1: CAPES -> OML
│   ├── eda_docentes_ufrpe.ipynb                     # Análise exploratória
│   └── api-scopus/                                  # Fase 2-4: coleta + validação Scopus
│       ├── gic_main.py            # Busca inicial por nome
│       ├── enrich_metrics.py      # Enriquece com citation/h-index
│       ├── recover_not_found.py   # Recupera not_found com queries alternativas
│       ├── auto_triage.py         # Triagem automática score>=100 / aff!=UFRPE
│       └── review_matches.py      # Revisão manual interativa
├── src/oml/                     # Saída OML (consumida por Rosetta/gic via Gradle)
└── Rosetta/gic/                 # Workspace OML (build.gradle, catalog.xml)
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

## Documentação adicional

- `scripts/api-scopus/README.md` — detalhes do pipeline Scopus
- `Sprint #2 - Codificar o Conhecimento.pdf` — escopo do sprint
- `Docente-e-Producao-Cientifica.pptx.pdf` — apresentação do projeto
