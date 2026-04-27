"""Triagem automatica pre-review_matches.py.

Decide review_status sem intervencao humana para os casos obvios:
- match_score >= 100  -> 'accepted' (afiliacao UFRPE garantida pela formula
  de score em gic_main.py: o teto sem afiliacao UFRPE eh 95).
- score < 100 e afiliacao nao menciona UFRPE/Pernambuco -> 'rejected'.
- restante                                           -> NA (revisao manual).

Saida alimenta review_matches.py --resume.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent

DEFAULT_INPUT = PROJECT_ROOT / "data" / "raw" / "scopus" / "scopus_ufrpe.csv"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "raw" / "scopus" / "scopus_ufrpe_reviewed.csv"

UFRPE_AFFIL_PATTERN = r"PERNAMBUCO|UFRPE"


def run(args: argparse.Namespace) -> int:
    df = pd.read_csv(args.input)
    if "review_status" not in df.columns:
        df["review_status"] = pd.NA
    df["review_status"] = df["review_status"].astype("object")

    matched = df["match_status"] == "matched"
    aff = df["scopus_affiliation_name"].fillna("").str.upper()
    has_ufrpe = aff.str.contains(UFRPE_AFFIL_PATTERN, regex=True)

    auto_accept = matched & (df["match_score"] >= args.accept_threshold)
    auto_reject = matched & (df["match_score"] < args.accept_threshold) & ~has_ufrpe
    untouched = df["review_status"].isna()

    df.loc[auto_accept & untouched, "review_status"] = "accepted"
    df.loc[auto_reject & untouched, "review_status"] = "rejected"

    args.output.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.output, index=False, encoding="utf-8")

    n_acc = (df["review_status"] == "accepted").sum()
    n_rej = (df["review_status"] == "rejected").sum()
    n_pend = (matched & df["review_status"].isna()).sum()
    print(f"accepted (auto, score >= {args.accept_threshold}): {n_acc}")
    print(f"rejected (auto, aff != UFRPE):                     {n_rej}")
    print(f"pendentes (revisao manual):                        {n_pend}")
    print(f"not_found (intactos):                              {(df['match_status']=='not_found').sum()}")
    print(f"saida: {args.output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Triagem automatica de matches Scopus.")
    p.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--accept-threshold", type=int, default=100,
                   help="Score >= este valor sao auto-aceitos (default: 100).")
    return p


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
