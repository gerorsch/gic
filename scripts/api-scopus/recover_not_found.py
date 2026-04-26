"""Fase 2: tenta recuperar docentes marcados como not_found usando variantes de query.

Estrategias, em ordem:
  1. AUTHOR-NAME("nome completo") AND AF-ID(<afid>) -- campo mais tolerante.
  2. AUTHLAST(last) AND AUTHFIRST(first) (sem AFFIL).
  3. Mesma da 2 mas compondo first com todos os tokens exceto o ultimo.
  4. Remove preposicoes ("de","da","do","dos","das") do sobrenome e retenta.

Ao encontrar um match (score minimo configuravel), grava no CSV com
match_status='found' e a query usada na coluna query_used.
"""
from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

import pandas as pd
import requests

from gic_main import _choose_best_candidate, _split_first_last_name
from gic_scopus_client import AuthorCandidate, ScopusAPIError, ScopusClient


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent

DEFAULT_INPUT = PROJECT_ROOT / "data" / "raw" / "scopus" / "scopus_ufrpe.csv"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "raw" / "scopus" / "scopus_ufrpe_recovered.csv"
DEFAULT_AFFILIATION_ID = "60000958"
PREPOSICOES = {"de", "da", "do", "dos", "das", "e"}


def _strip_preposicoes(name: str) -> str:
    tokens = [t for t in name.split() if t.lower() not in PREPOSICOES]
    return " ".join(tokens)


def _variant_queries(full_name: str, affiliation_id: str) -> list[tuple[str, str]]:
    first, last = _split_first_last_name(full_name)
    tokens = full_name.strip().split()
    middle_first = " ".join(tokens[:-1]) if len(tokens) >= 2 else first
    cleaned = _strip_preposicoes(full_name)
    cleaned_first, cleaned_last = _split_first_last_name(cleaned)

    variants: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(label: str, q: str) -> None:
        if q and q not in seen:
            seen.add(q)
            variants.append((label, q))

    add("last+first-no-affil", f"AUTHLAST({last}) AND AUTHFIRST({first})")
    if middle_first != first:
        add("last+compound-first", f"AUTHLAST({last}) AND AUTHFIRST({middle_first})")
    if cleaned != full_name:
        add("stripped-prep", f"AUTHLAST({cleaned_last}) AND AUTHFIRST({cleaned_first})")
    add("last-only+afid", f"AUTHLAST({last}) AND AF-ID({affiliation_id})")
    if cleaned != full_name:
        add("stripped-last-only+afid", f"AUTHLAST({cleaned_last}) AND AF-ID({affiliation_id})")

    return variants


def _run_query(
    client: ScopusClient,
    query: str,
    count: int,
    view: str,
) -> list[AuthorCandidate]:
    return client.search_authors(query=query, count=count, view=view)


def run(args: argparse.Namespace) -> int:
    input_path = args.input.resolve()
    output_path = args.output.resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"Arquivo nao encontrado: {input_path}")

    df = pd.read_csv(input_path)
    logger.info("CSV carregado: %s linhas", len(df))

    for col in ["scopus_author_id", "scopus_indexed_name", "scopus_affiliation_name", "query_used", "error_message"]:
        if col in df.columns:
            df[col] = df[col].astype("object")

    mask = df["match_status"] == "not_found"
    if args.limit:
        targets = df.index[mask].tolist()[: args.limit]
    else:
        targets = df.index[mask].tolist()

    logger.info("Docentes not_found a reprocessar: %s", len(targets))
    if not targets:
        df.to_csv(output_path, index=False, encoding="utf-8")
        logger.info("Nada a fazer. CSV copiado para %s", output_path)
        return 0

    client = ScopusClient(timeout_seconds=args.timeout_seconds)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    recovered = 0
    for count_idx, idx in enumerate(targets, start=1):
        full_name = df.at[idx, "author_name"]
        logger.info("[%s/%s] %s", count_idx, len(targets), full_name)

        variants = _variant_queries(full_name, args.affiliation_id)
        best_overall: tuple[AuthorCandidate | None, int, str, str] = (None, -1, "", "")

        for label, query in variants:
            try:
                candidates = _run_query(client, query, args.search_count, args.search_view)
            except (ScopusAPIError, requests.RequestException) as exc:
                logger.warning("  [%s] erro: %s", label, exc)
                continue

            best, score = _choose_best_candidate(
                candidates=candidates,
                target_name=full_name,
                affiliation_hint=args.affiliation_hint,
            )
            if best and score > best_overall[1]:
                best_overall = (best, score, label, query)

            if args.sleep_seconds > 0:
                time.sleep(args.sleep_seconds)

            if best_overall[1] >= args.early_accept_score:
                break

        best, score, label, query = best_overall
        if best and score >= args.min_score and best.author_id:
            df.at[idx, "scopus_author_id"] = best.author_id
            df.at[idx, "scopus_indexed_name"] = best.indexed_name
            df.at[idx, "scopus_affiliation_name"] = best.affiliation_name
            df.at[idx, "document_count"] = best.document_count
            df.at[idx, "match_status"] = "found"
            df.at[idx, "match_score"] = score
            df.at[idx, "query_used"] = f"[{label}] {query}"
            df.at[idx, "error_message"] = None
            recovered += 1
            logger.info("  -> recuperado via '%s' (score=%s, id=%s)", label, score, best.author_id)
        else:
            logger.info("  -> ainda not_found (melhor score=%s)", score)

        if args.flush_every > 0 and count_idx % args.flush_every == 0:
            df.to_csv(output_path, index=False, encoding="utf-8")
            logger.info("Checkpoint salvo em %s", output_path)

    df.to_csv(output_path, index=False, encoding="utf-8")

    found = (df["match_status"] == "found").sum()
    matched = (df["match_status"] == "matched").sum()
    not_found = (df["match_status"] == "not_found").sum()
    logger.info("Concluido. Recuperados nesta rodada: %s", recovered)
    logger.info(
        "Totais: matched=%s, found=%s, not_found=%s", matched, found, not_found
    )
    logger.info("CSV salvo em: %s", output_path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reprocessa docentes not_found com queries alternativas."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--affiliation-id", type=str, default=DEFAULT_AFFILIATION_ID)
    parser.add_argument(
        "--affiliation-hint",
        type=str,
        default="Universidade Federal Rural de Pernambuco",
        help="Usado na pontuacao dos candidatos.",
    )
    parser.add_argument("--search-view", type=str, default="STANDARD", choices=["STANDARD", "COMPLETE"])
    parser.add_argument("--search-count", type=int, default=25)
    parser.add_argument("--min-score", type=int, default=60, help="Score minimo para aceitar um match.")
    parser.add_argument("--early-accept-score", type=int, default=120, help="Se alcancado, aceita e pula demais variantes.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--sleep-seconds", type=float, default=0.2)
    parser.add_argument("--timeout-seconds", type=int, default=40)
    parser.add_argument("--flush-every", type=int, default=25)
    return parser


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
