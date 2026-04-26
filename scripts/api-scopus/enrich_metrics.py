"""Fase 1: enriquece CSV existente com citation_count e h_index via Author Retrieval.

Le data/raw/scopus/scopus_ufrpe.csv, percorre linhas com match_status='found'
(que ja tem scopus_author_id) e chama Author Retrieval para preencher as metricas.
Grava incrementalmente (checkpoint a cada N linhas).
"""
from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

import pandas as pd
import requests

from gic_scopus_client import ScopusAPIError, ScopusClient


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent

DEFAULT_INPUT = PROJECT_ROOT / "data" / "raw" / "scopus" / "scopus_ufrpe.csv"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "raw" / "scopus" / "scopus_ufrpe_enriched.csv"


def run(args: argparse.Namespace) -> int:
    input_path = args.input.resolve()
    output_path = args.output.resolve()

    if not input_path.exists():
        raise FileNotFoundError(f"Arquivo de entrada nao encontrado: {input_path}")

    df = pd.read_csv(input_path)
    logger.info("CSV carregado: %s linhas", len(df))

    if "scopus_author_id" not in df.columns:
        raise ValueError("CSV nao tem coluna scopus_author_id.")

    to_enrich_mask = df["match_status"].isin(["found", "matched"]) & df[
        "scopus_author_id"
    ].notna()

    if args.only_missing:
        to_enrich_mask &= df["citation_count"].isna() | df["h_index"].isna()

    indices = df.index[to_enrich_mask].tolist()
    if args.limit:
        indices = indices[: args.limit]

    logger.info("Autores a enriquecer: %s", len(indices))
    if not indices:
        logger.info("Nada para fazer. Encerrando.")
        return 0

    client = ScopusClient(timeout_seconds=args.timeout_seconds)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    processed = 0
    for idx in indices:
        author_id_raw = df.at[idx, "scopus_author_id"]
        try:
            author_id = str(int(float(author_id_raw)))
        except (TypeError, ValueError):
            author_id = str(author_id_raw).strip()

        name = df.at[idx, "author_name"]
        processed += 1
        logger.info("[%s/%s] %s (ID=%s)", processed, len(indices), name, author_id)

        try:
            metrics = client.get_author_metrics(author_id, view=args.retrieval_view)
            df.at[idx, "citation_count"] = metrics.citation_count
            df.at[idx, "h_index"] = metrics.h_index
            if metrics.document_count is not None:
                df.at[idx, "document_count"] = metrics.document_count
            if metrics.indexed_name and pd.isna(df.at[idx, "scopus_indexed_name"]):
                df.at[idx, "scopus_indexed_name"] = metrics.indexed_name
            if metrics.affiliation_name:
                df.at[idx, "scopus_affiliation_name"] = metrics.affiliation_name
            df.at[idx, "match_status"] = "matched"
            df.at[idx, "error_message"] = None
        except (ScopusAPIError, requests.RequestException) as exc:
            df.at[idx, "match_status"] = "api_error"
            df.at[idx, "error_message"] = str(exc)
            if args.stop_on_error:
                df.to_csv(output_path, index=False, encoding="utf-8")
                raise
        except Exception as exc:
            df.at[idx, "match_status"] = "error"
            df.at[idx, "error_message"] = str(exc)
            if args.stop_on_error:
                df.to_csv(output_path, index=False, encoding="utf-8")
                raise

        if args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)

        if args.flush_every > 0 and processed % args.flush_every == 0:
            df.to_csv(output_path, index=False, encoding="utf-8")
            logger.info("Checkpoint salvo em %s", output_path)

    df.to_csv(output_path, index=False, encoding="utf-8")

    matched = (df["match_status"] == "matched").sum()
    not_found = (df["match_status"] == "not_found").sum()
    errors = df["match_status"].isin(["api_error", "error"]).sum()
    logger.info("Concluido. Arquivo salvo em: %s", output_path)
    logger.info("Resumo: matched=%s, not_found=%s, errors=%s", matched, not_found, errors)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Enriquece CSV com citation_count e h_index via Author Retrieval."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="CSV de entrada (saida da Fase 0).")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="CSV de saida enriquecido.")
    parser.add_argument(
        "--retrieval-view",
        type=str,
        default="METRICS",
        choices=["LIGHT", "STANDARD", "ENHANCED", "METRICS", "ENTITLED"],
    )
    parser.add_argument("--only-missing", action="store_true", help="So processa linhas sem citation_count/h_index.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--sleep-seconds", type=float, default=0.2)
    parser.add_argument("--timeout-seconds", type=int, default=40)
    parser.add_argument("--flush-every", type=int, default=25)
    parser.add_argument("--stop-on-error", action="store_true")
    return parser


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
