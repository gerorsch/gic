# Coleta de Metricas Scopus por Docente (UFRPE)

Pipeline que consulta a API Elsevier/Scopus para enriquecer a lista CAPES de
docentes UFRPE com `scopus_author_id`, `citation_count`, `h_index` e
`document_count`. Saida final alimenta `scripts/generate_oml_from_capes_docentes_ufrpe.py`.

## Arquivos

| Arquivo | Papel |
|---|---|
| `gic_main.py` | Fase 0 - busca inicial por nome (Author Search). |
| `enrich_metrics.py` | Fase 1 - preenche metricas via Author Retrieval. |
| `recover_not_found.py` | Fase 2 - reprocessa `not_found` com queries alternativas. |
| `review_matches.py` | Fase 3 - revisao manual interativa de matches suspeitos. |
| `gic_scopus_client.py` | Cliente comum da API Elsevier. |

## Configuracao

1. `ELSEVIER_API_KEY` em `.env` (raiz do projeto ou em `scripts/api-scopus/.env`):
   ```env
   ELSEVIER_API_KEY=SUA_CHAVE
   # Opcional:
   ELSEVIER_INSTTOKEN=SEU_TOKEN
   ```
2. Dependencias:
   ```bash
   pip install pandas requests python-dotenv
   ```
3. Limites da API pessoal Elsevier: **5000/semana por endpoint** (Search e
   Retrieval contam separadamente). `COMPLETE` view so funciona com INSTTOKEN.

## Pipeline

### Fase 0 - busca por nome

```bash
uv run gic_main.py --skip-metrics --overwrite
```

Consome ~1 Author Search por docente (~826 chamadas).
Gera `data/raw/scopus/scopus_ufrpe.csv` com colunas basicas e
`match_status` em `found` / `not_found`.

### Fase 1 - enriquecer com metricas

```bash
uv run enrich_metrics.py
```

Consome ~1 Author Retrieval por linha `found` (~680 chamadas).
Gera `scopus_ufrpe_enriched.csv` com `citation_count` e `h_index`
preenchidos; `match_status` vira `matched`.

### Fase 2 - recuperar not_found

```bash
uv run recover_not_found.py --input ../../data/raw/scopus/scopus_ufrpe_enriched.csv
```

Tenta variantes de query (sem AFFIL, sobrenome apenas, preposicoes removidas)
para os ~146 `not_found`. Consome ate ~4 Author Search por docente.
Gera `scopus_ufrpe_recovered.csv`.

### Fase 1 (novamente) - metricas dos recuperados

```bash
uv run enrich_metrics.py \
  --input ../../data/raw/scopus/scopus_ufrpe_recovered.csv \
  --output ../../data/raw/scopus/scopus_ufrpe.csv \
  --only-missing
```

Grava a saida final em `scopus_ufrpe.csv` (nome que o pipeline downstream espera).

### Fase 3 - revisao manual (opcional)

```bash
uv run review_matches.py
```

Interface interativa: abre perfil Scopus no navegador, pergunta accept/reject.
Cria coluna `review_status`.

## Estrutura do CSV final

| Coluna | Descricao |
|---|---|
| `author_name` | Nome CAPES original |
| `scopus_author_id` | ID Scopus |
| `scopus_indexed_name` | Nome padronizado no Scopus |
| `scopus_affiliation_name` | Afiliacao atual |
| `document_count` | Nº de documentos |
| `citation_count` | Total de citacoes |
| `h_index` | Indice h |
| `match_status` | `matched` / `not_found` / `api_error` / `error` |
| `match_score` | Confianca do matching (0-155+) |
| `query_used` | Query enviada a API |
| `error_message` | Detalhe de erro |
| `review_status` | (pos-revisao) `accepted` / `rejected` |

## Observacoes

- Cada script grava checkpoints incrementais (configuravel com `--flush-every`);
  interromper no meio nao perde progresso.
- Para rodar em ambientes que suspendem o laptop, use `tmux` / `screen` ou
  `systemd-inhibit --what=handle-lid-switch` em outro terminal.
- Revisao manual recomendada para `match_score < 100` (clusters comuns: mesma
  sigla de sobrenome em outra universidade pernambucana).
