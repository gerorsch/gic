from __future__ import annotations

import argparse
import logging
import re
import time
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from gic_main import _detect_delimiter, _load_docente_names, _split_first_last_name
from gic_scopus_client import (
    AffiliationCandidate,
    AuthorCandidate,
    ScopusAPIError,
    ScopusClient,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent

DEFAULT_INPUT = PROJECT_ROOT / "data" / "processed" / "docentes_ufrpe.csv"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "raw" / "scopus" / "scopus_ufrpe_by_affiliation.csv"
DEFAULT_AFFILIATION_QUERY = "Universidade Federal Rural de Pernambuco"


def _normalize_text(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", value)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^a-z\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _resolve_affiliation_id(
    client: ScopusClient,
    explicit_id: str | None,
    query: str,
) -> AffiliationCandidate:
    if explicit_id:
        logger.info("Usando AF-ID explicito: %s", explicit_id)
        return AffiliationCandidate(affiliation_id=explicit_id, name=query)

    logger.info("Buscando afiliacao por: %s", query)
    scopus_query = f'AFFIL("{query}")'
    candidates = client.search_affiliation(query=scopus_query, count=25)
    if not candidates:
        raise RuntimeError(f"Nenhuma afiliacao encontrada para query: {query}")

    target_norm = _normalize_text(query)
    best, best_score = None, -1.0
    for cand in candidates:
        score = SequenceMatcher(None, target_norm, _normalize_text(cand.name)).ratio()
        if cand.document_count:
            score += min(cand.document_count, 20000) / 200000
        if score > best_score:
            best, best_score = cand, score

    if not best or not best.affiliation_id:
        raise RuntimeError(f"Nao foi possivel resolver AF-ID para: {query}")

    logger.info(
        "Afiliacao selecionada: %s (AF-ID=%s, docs=%s)",
        best.name,
        best.affiliation_id,
        best.document_count,
    )
    return best


def _build_docente_index(names: list[str]) -> dict[str, str]:
    index: dict[str, str] = {}
    for original in names:
        index[_normalize_text(original)] = original
    return index


def _match_docente(
    candidate: AuthorCandidate,
    docente_index: dict[str, str],
) -> tuple[str | None, float]:
    if not docente_index:
        return None, 0.0

    full = " ".join(
        part for part in [candidate.given_name, candidate.surname] if part
    ).strip()
    cand_full_norm = _normalize_text(full) if full else ""
    cand_indexed_norm = _normalize_text(candidate.indexed_name)

    best_name, best_score = None, 0.0
    for docente_norm, original in docente_index.items():
        if not docente_norm:
            continue
        for cand_norm in (cand_full_norm, cand_indexed_norm):
            if not cand_norm:
                continue
            ratio = SequenceMatcher(None, docente_norm, cand_norm).ratio()
            last_token = docente_norm.split()[-1] if docente_norm else ""
            cand_surname_norm = _normalize_text(candidate.surname)
            if last_token and last_token == cand_surname_norm:
                ratio += 0.1
            if ratio > best_score:
                best_score, best_name = ratio, original

    return best_name, best_score


def _author_row_base(candidate: AuthorCandidate) -> dict[str, Any]:
    full = " ".join(
        part for part in [candidate.given_name, candidate.surname] if part
    ).strip()
    return {
        "scopus_author_id": candidate.author_id,
        "scopus_indexed_name": candidate.indexed_name,
        "scopus_given_name": candidate.given_name,
        "scopus_surname": candidate.surname,
        "scopus_full_name": full or candidate.indexed_name,
        "scopus_affiliation_name": candidate.affiliation_name,
        "document_count": candidate.document_count,
        "citation_count": None,
        "h_index": None,
        "match_docente": None,
        "match_score": None,
        "metrics_status": None,
        "error_message": None,
    }


def run(args: argparse.Namespace) -> int:
    output_path = args.output.resolve()
    if output_path.exists() and not args.overwrite:
        raise FileExistsError(
            f"Arquivo de saida ja existe: {output_path}. Use --overwrite."
        )

    docente_index: dict[str, str] = {}
    if args.docentes_csv and args.docentes_csv.exists():
        names = _load_docente_names(args.docentes_csv.resolve(), args.name_column)
        docente_index = _build_docente_index(names)
        logger.info("Docentes carregados para cruzamento: %s", len(docente_index))

    client = ScopusClient(timeout_seconds=args.timeout_seconds)

    affiliation = _resolve_affiliation_id(
        client=client,
        explicit_id=args.affiliation_id,
        query=args.affiliation_query,
    )

    if args.dry_run:
        logger.info("dry-run: afiliacao resolvida. Encerrando sem paginar autores.")
        return 0

    records: list[dict[str, Any]] = []
    processed = 0
    for candidate in client.iter_authors_by_affiliation(
        affiliation_id=affiliation.affiliation_id,
        page_size=args.page_size,
        max_results=args.limit,
        view=args.search_view,
    ):
        processed += 1
        row = _author_row_base(candidate)
        row["scopus_affiliation_id"] = affiliation.affiliation_id

        if docente_index:
            match_name, match_score = _match_docente(candidate, docente_index)
            row["match_docente"] = match_name
            row["match_score"] = round(match_score, 4)

        if not args.skip_metrics and candidate.author_id:
            try:
                metrics = client.get_author_metrics(
                    candidate.author_id, view=args.retrieval_view
                )
                row["citation_count"] = metrics.citation_count
                row["h_index"] = metrics.h_index
                if metrics.document_count is not None:
                    row["document_count"] = metrics.document_count
                if metrics.indexed_name:
                    row["scopus_indexed_name"] = (
                        row["scopus_indexed_name"] or metrics.indexed_name
                    )
                if metrics.affiliation_name:
                    row["scopus_affiliation_name"] = (
                        row["scopus_affiliation_name"] or metrics.affiliation_name
                    )
                row["metrics_status"] = "ok"
            except (ScopusAPIError, requests.RequestException) as exc:
                row["metrics_status"] = "api_error"
                row["error_message"] = str(exc)
                if args.stop_on_error:
                    records.append(row)
                    break
            except Exception as exc:
                row["metrics_status"] = "error"
                row["error_message"] = str(exc)
                if args.stop_on_error:
                    records.append(row)
                    break

            if args.sleep_seconds > 0:
                time.sleep(args.sleep_seconds)

        records.append(row)
        if processed % 50 == 0:
            logger.info("Autores processados: %s", processed)

    logger.info("Total de autores coletados: %s", processed)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(records).to_csv(output_path, index=False, encoding="utf-8")
    logger.info("Arquivo salvo em: %s", output_path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Coleta autores e metricas do Scopus via afiliacao (AF-ID). "
            "Opcionalmente cruza com o CSV de docentes CAPES."
        )
    )
    parser.add_argument(
        "--affiliation-id",
        type=str,
        default=None,
        help="AF-ID da afiliacao (se conhecido). Ignora --affiliation-query.",
    )
    parser.add_argument(
        "--affiliation-query",
        type=str,
        default=DEFAULT_AFFILIATION_QUERY,
        help="Nome da afiliacao para resolver o AF-ID via Affiliation Search.",
    )
    parser.add_argument(
        "--docentes-csv",
        type=Path,
        default=DEFAULT_INPUT,
        help="CSV de docentes para cruzamento (opcional). Use vazio para pular.",
    )
    parser.add_argument(
        "--name-column",
        type=str,
        default=None,
        help="Nome da coluna de docente (default: NM_DOCENTE).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="CSV final de saida.",
    )
    parser.add_argument("--search-view", type=str, default="STANDARD", choices=["STANDARD", "COMPLETE"])
    parser.add_argument(
        "--retrieval-view",
        type=str,
        default="METRICS",
        choices=["LIGHT", "STANDARD", "ENHANCED", "METRICS", "ENTITLED"],
    )
    parser.add_argument("--page-size", type=int, default=200, help="Autores por pagina na Author Search.")
    parser.add_argument("--limit", type=int, default=None, help="Limita total de autores processados.")
    parser.add_argument("--sleep-seconds", type=float, default=0.2, help="Pausa entre consultas de metricas.")
    parser.add_argument("--timeout-seconds", type=int, default=40)
    parser.add_argument("--skip-metrics", action="store_true", help="Nao chama Author Retrieval; usa apenas Author Search.")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Apenas resolve o AF-ID e encerra.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
