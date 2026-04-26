from __future__ import annotations

import argparse
import csv
import logging
import re
import time
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from gic_scopus_client import AuthorCandidate, ScopusAPIError, ScopusClient


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent

DEFAULT_INPUT = PROJECT_ROOT / "data" / "processed" / "docentes_ufrpe.csv"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "raw" / "scopus" / "scopus_ufrpe.csv"
DEFAULT_AFFILIATION = "Universidade Federal Rural de Pernambuco"


def _normalize_text(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", value)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^a-z\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _split_first_last_name(full_name: str) -> tuple[str, str]:
    tokens = [tok for tok in full_name.strip().split() if tok]
    if len(tokens) < 2:
        return full_name.strip(), full_name.strip()
    return tokens[0], tokens[-1]


def _detect_delimiter(csv_path: Path) -> str:
    with csv_path.open("r", encoding="utf-8", errors="ignore") as handle:
        sample = handle.read(8192)
    try:
        return csv.Sniffer().sniff(sample, delimiters=";,|\t").delimiter
    except csv.Error:
        return ","


def _load_docente_names(input_csv: Path, name_column: str | None) -> list[str]:
    if not input_csv.exists():
        raise FileNotFoundError(f"Arquivo de entrada nao encontrado: {input_csv}")

    delimiter = _detect_delimiter(input_csv)
    try:
        df = pd.read_csv(
            input_csv,
            sep=delimiter,
            encoding="utf-8",
            dtype=str,
            low_memory=False,
        )
    except UnicodeDecodeError:
        df = pd.read_csv(
            input_csv,
            sep=delimiter,
            encoding="iso-8859-1",
            dtype=str,
            low_memory=False,
        )

    candidate_columns = [name_column] if name_column else ["NM_DOCENTE", "author_name", "nome", "NOME"]
    selected_column = next((col for col in candidate_columns if col and col in df.columns), None)
    if not selected_column:
        raise ValueError(
            f"Nenhuma coluna de nome encontrada. Colunas disponiveis: {list(df.columns)}"
        )

    names = (
        df[selected_column]
        .dropna()
        .astype(str)
        .str.strip()
    )
    names = names[names != ""].drop_duplicates()
    return names.tolist()


def _candidate_full_name(candidate: AuthorCandidate) -> str:
    if candidate.given_name or candidate.surname:
        return " ".join(part for part in [candidate.given_name, candidate.surname] if part)
    if candidate.indexed_name:
        return candidate.indexed_name
    return ""


def _score_candidate(
    candidate: AuthorCandidate,
    target_name: str,
    affiliation_hint: str | None,
) -> int:
    score = 0
    target_norm = _normalize_text(target_name)
    candidate_norm = _normalize_text(_candidate_full_name(candidate))

    if target_norm and candidate_norm:
        sim_ratio = SequenceMatcher(None, target_norm, candidate_norm).ratio()
        score += int(sim_ratio * 40)

    target_first, target_last = _split_first_last_name(target_name)
    if _normalize_text(candidate.given_name) == _normalize_text(target_first):
        score += 20
    if _normalize_text(candidate.surname) == _normalize_text(target_last):
        score += 30

    if affiliation_hint:
        aff_norm = _normalize_text(candidate.affiliation_name)
        if aff_norm and _normalize_text(affiliation_hint) in aff_norm:
            score += 60

    if candidate.document_count is not None:
        score += min(candidate.document_count, 100) // 20

    return score


def _choose_best_candidate(
    candidates: list[AuthorCandidate],
    target_name: str,
    affiliation_hint: str | None,
) -> tuple[AuthorCandidate | None, int]:
    if not candidates:
        return None, 0

    best_candidate = None
    best_score = -1
    for cand in candidates:
        score = _score_candidate(cand, target_name=target_name, affiliation_hint=affiliation_hint)
        if score > best_score:
            best_candidate = cand
            best_score = score
    return best_candidate, best_score


def _build_record_base(author_name: str) -> dict[str, Any]:
    return {
        "author_name": author_name,
        "citation_count": None,
        "h_index": None,
        "document_count": None,
        "scopus_author_id": None,
        "scopus_indexed_name": None,
        "scopus_affiliation_name": None,
        "match_status": None,
        "match_score": None,
        "query_used": None,
        "error_message": None,
    }


def _flush_records(records: list[dict[str, Any]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(records).to_csv(output_path, index=False, encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    input_path = args.input.resolve()
    output_path = args.output.resolve()

    if output_path.exists() and not args.overwrite:
        raise FileExistsError(
            f"Arquivo de saida ja existe: {output_path}. Use --overwrite para sobrescrever."
        )

    names = _load_docente_names(input_path, args.name_column)
    if args.offset:
        names = names[args.offset:]
    if args.limit is not None:
        names = names[:args.limit]

    logger.info("Docentes carregados para consulta Scopus: %s", len(names))
    if not names:
        raise ValueError("Nenhum docente encontrado para consulta.")

    client = None if args.dry_run else ScopusClient(timeout_seconds=args.timeout_seconds)
    records: list[dict[str, Any]] = []

    for index, full_name in enumerate(names, start=1):
        logger.info("[%s/%s] Processando docente: %s", index, len(names), full_name)
        row = _build_record_base(full_name)

        if args.dry_run:
            row["match_status"] = "dry_run"
            records.append(row)
            continue

        first_name, last_name = _split_first_last_name(full_name)

        try:
            query_used, candidates = client.search_author_by_name(
                first_name=first_name,
                last_name=last_name,
                affiliation_hint=args.affiliation_hint,
                count=args.search_count,
                view=args.search_view,
            )

            row["query_used"] = query_used

            best, score = _choose_best_candidate(
                candidates=candidates,
                target_name=full_name,
                affiliation_hint=args.affiliation_hint,
            )

            if not best or not best.author_id:
                row["match_status"] = "not_found"
                records.append(row)
                continue

            row.update(
                {
                    "scopus_author_id": best.author_id,
                    "scopus_indexed_name": best.indexed_name,
                    "scopus_affiliation_name": best.affiliation_name,
                    "document_count": best.document_count,
                    "match_score": score,
                }
            )

            if args.skip_metrics:
                row["match_status"] = "found"
                records.append(row)
            else:
                metrics = client.get_author_metrics(best.author_id, view=args.retrieval_view)
                row.update(
                    {
                        "citation_count": metrics.citation_count,
                        "h_index": metrics.h_index,
                        "document_count": metrics.document_count if metrics.document_count is not None else best.document_count,
                        "scopus_author_id": metrics.author_id,
                        "scopus_indexed_name": metrics.indexed_name or best.indexed_name,
                        "scopus_affiliation_name": metrics.affiliation_name or best.affiliation_name,
                        "match_status": "matched",
                    }
                )
                records.append(row)

        except (ScopusAPIError, requests.RequestException) as exc:
            row["match_status"] = "api_error"
            row["error_message"] = str(exc)
            records.append(row)
            if args.stop_on_error:
                raise
        except Exception as exc:
            row["match_status"] = "error"
            row["error_message"] = str(exc)
            records.append(row)
            if args.stop_on_error:
                raise

        if args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)

        if args.flush_every > 0 and len(records) % args.flush_every == 0:
            _flush_records(records, output_path)
            logger.info("Checkpoint salvo (%s linhas) em %s", len(records), output_path)

    _flush_records(records, output_path)
    output_df = pd.DataFrame(records)

    matched = (output_df["match_status"] == "matched").sum()
    not_found = (output_df["match_status"] == "not_found").sum()
    errors = output_df["match_status"].isin(["api_error", "error"]).sum()

    logger.info("Concluido. Arquivo salvo em: %s", output_path)
    logger.info("Resumo: matched=%s, not_found=%s, errors=%s", matched, not_found, errors)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Coleta metricas do Scopus por docente e gera CSV de enriquecimento "
            "para o pipeline docentes_ufrpe."
        )
    )

    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="CSV de docentes de entrada.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="CSV final no formato esperado pelo pipeline.")
    parser.add_argument("--name-column", type=str, default=None, help="Nome da coluna de docente (default: NM_DOCENTE).")
    parser.add_argument("--affiliation-hint", type=str, default=DEFAULT_AFFILIATION, help="Afiliacao usada para melhorar matching de autor.")
    parser.add_argument("--search-view", type=str, default="STANDARD", choices=["STANDARD", "COMPLETE"], help="View da Author Search API.")
    parser.add_argument("--retrieval-view", type=str, default="METRICS", choices=["LIGHT", "STANDARD", "ENHANCED", "METRICS", "ENTITLED"], help="View da Author Retrieval API.")
    parser.add_argument("--search-count", type=int, default=25, help="Quantidade de candidatos por busca de autor.")
    parser.add_argument("--offset", type=int, default=0, help="Indice inicial dos docentes para retomar execucao.")
    parser.add_argument("--limit", type=int, default=None, help="Limita o numero de docentes processados.")
    parser.add_argument("--sleep-seconds", type=float, default=0.2, help="Pausa entre consultas para reduzir chance de rate limit.")
    parser.add_argument("--timeout-seconds", type=int, default=40, help="Timeout HTTP por requisicao.")
    parser.add_argument("--overwrite", action="store_true", help="Sobrescreve o CSV de saida caso ja exista.")
    parser.add_argument("--stop-on-error", action="store_true", help="Interrompe ao primeiro erro.")
    parser.add_argument("--dry-run", action="store_true", help="Nao chama API; apenas valida leitura e gera estrutura de saida.")
    parser.add_argument("--skip-metrics", action="store_true", help="Pula Author Retrieval; grava apenas resultados da Author Search (economiza quota de Retrieval).")
    parser.add_argument("--flush-every", type=int, default=25, help="Grava CSV parcial a cada N linhas (checkpoint). 0 desativa.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
