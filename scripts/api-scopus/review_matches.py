"""Revisao manual interativa de matches suspeitos.

Percorre linhas com match_status='matched' e match_score numa faixa configuravel,
abre o perfil Scopus no navegador e pergunta se o match esta correto.
Grava a decisao em uma nova coluna 'review_status' (accepted/rejected).

Uso tipico:
    uv run review_matches.py \\
        --input ../../data/raw/scopus/scopus_ufrpe.csv \\
        --output ../../data/raw/scopus/scopus_ufrpe_reviewed.csv \\
        --min-score 60 --max-score 99

Comandos durante a revisao:
    a - aceitar match
    r - rejeitar match
    s - pular (deixar review_status vazio, revisar depois)
    o - reabrir pagina no navegador
    b - voltar e alterar decisao anterior
    q - sair e salvar
    ? - ajuda
"""
from __future__ import annotations

import argparse
import webbrowser
from pathlib import Path

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent

DEFAULT_INPUT = PROJECT_ROOT / "data" / "raw" / "scopus" / "scopus_ufrpe.csv"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "raw" / "scopus" / "scopus_ufrpe_reviewed.csv"
SCOPUS_URL = "https://www.scopus.com/authid/detail.uri?authorId={author_id}"

HELP_TEXT = """
Comandos:
  a  - aceitar match
  r  - rejeitar match
  s  - pular (sem decidir; pode revisar depois)
  o  - reabrir pagina Scopus no navegador
  b  - voltar e mudar ultima decisao
  q  - salvar e sair
  ?  - este help
"""


def _fmt_value(v) -> str:
    if pd.isna(v):
        return "-"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _show_row(df: pd.DataFrame, idx: int, pos: int, total: int) -> None:
    row = df.loc[idx]
    print("=" * 72)
    print(f"[{pos}/{total}]  CAPES: {row['author_name']}")
    print(f"  Scopus ID       : {_fmt_value(row.get('scopus_author_id'))}")
    print(f"  Indexed name    : {_fmt_value(row.get('scopus_indexed_name'))}")
    print(f"  Affiliation     : {_fmt_value(row.get('scopus_affiliation_name'))}")
    print(f"  match_score     : {_fmt_value(row.get('match_score'))}")
    print(f"  document_count  : {_fmt_value(row.get('document_count'))}")
    print(f"  h_index         : {_fmt_value(row.get('h_index'))}")
    print(f"  citation_count  : {_fmt_value(row.get('citation_count'))}")
    prev = row.get("review_status")
    if pd.notna(prev):
        print(f"  [ja revisado: {prev}]")
    print("-" * 72)


def _open_browser(author_id) -> None:
    try:
        aid = str(int(float(author_id)))
    except (TypeError, ValueError):
        aid = str(author_id).strip()
    url = SCOPUS_URL.format(author_id=aid)
    print(f"Abrindo: {url}")
    try:
        webbrowser.open(url, new=2)
    except Exception as exc:
        print(f"(falha ao abrir: {exc} — abra manualmente o link acima)")


def run(args: argparse.Namespace) -> int:
    input_path = args.input.resolve()
    output_path = args.output.resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"Arquivo nao encontrado: {input_path}")

    source_path = output_path if output_path.exists() and args.resume else input_path
    df = pd.read_csv(source_path)
    print(f"CSV carregado de: {source_path} ({len(df)} linhas)")

    if "review_status" not in df.columns:
        df["review_status"] = pd.NA
    df["review_status"] = df["review_status"].astype("object")

    mask = (df["match_status"] == "matched") & (df["match_score"] >= args.min_score) & (
        df["match_score"] <= args.max_score
    )
    if not args.include_reviewed:
        mask &= df["review_status"].isna()

    indices = df.index[mask].tolist()
    total = len(indices)
    print(f"A revisar: {total} matches com score entre {args.min_score} e {args.max_score}")
    print(HELP_TEXT)

    if total == 0:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(output_path, index=False, encoding="utf-8")
        print("Nada a revisar. CSV salvo.")
        return 0

    history: list[tuple[int, object]] = []
    i = 0
    while i < total:
        idx = indices[i]
        _show_row(df, idx, i + 1, total)
        if not args.no_browser:
            _open_browser(df.at[idx, "scopus_author_id"])

        while True:
            ans = input("[a/r/s/o/b/q/?]> ").strip().lower()
            if ans in ("a", "aceitar", "accept"):
                history.append((idx, df.at[idx, "review_status"]))
                df.at[idx, "review_status"] = "accepted"
                break
            if ans in ("r", "rejeitar", "reject"):
                history.append((idx, df.at[idx, "review_status"]))
                df.at[idx, "review_status"] = "rejected"
                break
            if ans in ("s", "skip", "pular"):
                history.append((idx, df.at[idx, "review_status"]))
                break
            if ans == "o":
                _open_browser(df.at[idx, "scopus_author_id"])
                continue
            if ans == "b":
                if not history:
                    print("Sem decisao anterior para desfazer.")
                    continue
                i -= 1
                prev_idx, prev_val = history.pop()
                df.at[prev_idx, "review_status"] = prev_val
                print(f"Revertido. Voltando para {i+1}/{total}.")
                i -= 1
                break
            if ans in ("q", "quit", "sair"):
                output_path.parent.mkdir(parents=True, exist_ok=True)
                df.to_csv(output_path, index=False, encoding="utf-8")
                print(f"Progresso salvo em {output_path}. Saindo.")
                return 0
            if ans == "?":
                print(HELP_TEXT)
                continue
            print("Comando invalido. Digite ? para ajuda.")

        i += 1
        if args.flush_every > 0 and i % args.flush_every == 0:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(output_path, index=False, encoding="utf-8")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False, encoding="utf-8")

    accepted = (df["review_status"] == "accepted").sum()
    rejected = (df["review_status"] == "rejected").sum()
    print(f"\nRevisao concluida. accepted={accepted}, rejected={rejected}")
    print(f"CSV salvo em: {output_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Revisao manual interativa de matches Scopus.")
    p.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--min-score", type=int, default=60)
    p.add_argument("--max-score", type=int, default=99)
    p.add_argument("--resume", action="store_true", help="Continua de onde parou (le do --output se existir).")
    p.add_argument("--include-reviewed", action="store_true", help="Tambem mostra linhas ja revisadas.")
    p.add_argument("--no-browser", action="store_true", help="Nao abre o navegador automaticamente.")
    p.add_argument("--flush-every", type=int, default=10, help="Grava CSV a cada N decisoes.")
    return p


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
