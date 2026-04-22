# Coleta de Metricas Scopus por Docente (UFRPE)

Este modulo consulta a API da Elsevier/Scopus para obter metricas por autor e
gera o arquivo CSV consumido pelo pipeline:

- `scripts/generate_oml_from_capes_docentes_ufrpe.py`

## Objetivo

Partindo da lista de docentes (CAPES), gerar:

- `data/raw/scopus/scopus_ufrpe.csv`

Com as colunas minimas esperadas:

- `author_name`
- `citation_count`
- `h_index`
- `document_count`

## Como Funciona

1. Le o CSV de docentes (por padrao `data/processed/docentes_ufrpe.csv`).
2. Faz Author Search no Scopus por nome (`AUTHFIRST + AUTHLAST`), priorizando
   afiliacao UFRPE.
3. Seleciona o melhor candidato de autor.
4. Faz Author Retrieval (`view=METRICS`) para coletar citacoes/h-index/documentos.
5. Salva o CSV final em `data/raw/scopus/scopus_ufrpe.csv`.

## Configuracao

Crie/edite `.env` (na raiz do projeto ou em `scripts/api-scopus/.env`) com:

```env
ELSEVIER_API_KEY=SUA_CHAVE_AQUI
```

Opcional:

```env
ELSEVIER_INSTTOKEN=SEU_INSTTOKEN
```

## Dependencias

```bash
pip install pandas requests python-dotenv
```

## Execucao

Na raiz do projeto:

```bash
python scripts/api-scopus/gic_main.py --overwrite
```

Executar amostra pequena:

```bash
python scripts/api-scopus/gic_main.py --limit 20 --overwrite
```

Retomar a partir de um indice:

```bash
python scripts/api-scopus/gic_main.py --offset 200 --limit 100 --overwrite
```

Validar fluxo sem chamar API:

```bash
python scripts/api-scopus/gic_main.py --dry-run --limit 10 --overwrite
```

## Parametros Uteis

- `--input`: CSV de entrada de docentes.
- `--output`: CSV final (default `data/raw/scopus/scopus_ufrpe.csv`).
- `--name-column`: coluna de nome do docente (default detecta `NM_DOCENTE`).
- `--affiliation-hint`: afiliacao para melhorar o matching.
- `--search-view`: `STANDARD` ou `COMPLETE` na Author Search API.
- `--retrieval-view`: `METRICS` (default), `LIGHT`, `STANDARD`, `ENHANCED`, `ENTITLED`.
- `--sleep-seconds`: pausa entre requisicoes.
- `--stop-on-error`: aborta no primeiro erro.

## Observacoes

- A qualidade do matching por nome pode variar para homonimos.
- Caso necessario, ajuste `--affiliation-hint` para refinar a busca.
- O CSV final inclui colunas adicionais de rastreabilidade (`match_status`,
  `match_score`, `query_used`, `error_message`).
