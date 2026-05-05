#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
  SCRIPT: Gerador de OML — Docentes UFRPE e Produção Científica

  Pipeline que transforma dados de DOCENTES da CAPES + métricas do Scopus
  em uma ontologia OML para análise da relação entre tempo de carreira
  e impacto científico.

  CONTEXTO (Sprint 1):
    Investigar a relação entre tempo de carreira acadêmica (anos desde o
    doutorado) e impacto científico (citações) dos docentes da UFRPE.

  ENTRADA:
    • CSVs de DOCENTES da CAPES (2021-2024) em data/raw/capes_docentes/
      Colunas esperadas (dataset "Docentes da Pós-Graduação Stricto Sensu"):
        - NM_DOCENTE
        - AN_TITULACAO_DOCENTE  (ano de obtenção do doutorado)
        - CD_ENTIDADE_CAPES / SG_ENTIDADE_ENSINO
        - NM_PROGRAMA_IES / CD_PROGRAMA_IES
        - NM_AREA_CONHECIMENTO
        - DS_CATEGORIA_DOCENTE  (PERMANENTE, COLABORADOR, VISITANTE)
        - DS_TIPO_DOCUMENTO_DOCENTE  (DOUTORADO, MESTRADO, etc.)

    • (Opcional) CSV de métricas Scopus em data/raw/scopus/scopus_ufrpe.csv
      Colunas esperadas:
        - author_name
        - citation_count
        - h_index
        - document_count

  SAÍDA:
    • docentes-ufrpe.oml   — Ontologia com instâncias de Docente
    • docentes_ufrpe.csv    — Dados processados para análise estatística

  AUTORES:
    José Rafael e Stella
    Disciplina: Gestão da Informação e do Conhecimento
    UFRPE - 2025
================================================================================
"""

import os
import sys
import re
import unicodedata
import pandas as pd
import logging
from pathlib import Path
from typing import List, Dict, Tuple, Optional, Any
from dataclasses import dataclass, field
from collections import defaultdict


# ============================================================================
# CONFIGURAÇÃO
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DOCENTES_DIR = PROJECT_ROOT / "data" / "raw" / "capes_docentes"
DATA_SCOPUS_DIR = PROJECT_ROOT / "data" / "raw" / "scopus"
DATA_PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
OML_OUTPUT_DIR = PROJECT_ROOT / "Rosetta" / "gic" / "src" / "oml" / "gic.ufrpe.br" / "cti" / "description"

# ── Parâmetros de Filtragem ──────────────────────────────────────────────────
# Ajuste estes valores conforme a necessidade:

FILTRO_SIGLA_IES = "UFRPE"          # Sigla da instituição alvo
FILTRO_TIPO_TITULACAO = "DOUTORADO" # Apenas doutores
FILTRO_CATEGORIAS = [               # Categorias de vínculo desejadas
    "PERMANENTE",
    "COLABORADOR",
]

# ── URIs e Namespaces OML ────────────────────────────────────────────────────
VOCABULARY_URI = "http://gic.ufrpe.br/cti/vocabulary/cti"
VOCABULARY_NS = "cti"
DESCRIPTION_URI = "http://gic.ufrpe.br/cti/description/docentes-ufrpe"
DC_URI = "http://purl.org/dc/elements/1.1/"
DC_NS = "dc"


# ============================================================================
# DATACLASSES
# ============================================================================

@dataclass
class DocenteInstance:
    """
    Representa um DOCENTE com doutorado vinculado à UFRPE.

    Identidade pelo ID_PESSOA (CAPES) — um docente pode estar vinculado a
    múltiplos PPGs simultaneamente; a relação fica em ``vinculados_a``.

    Área e programa NÃO são propriedades do Docente — pertencem ao PPG.
    """
    id: str
    nm_docente: str
    an_titulacao: int
    ds_categoria: str
    vinculados_a: List[str] = field(default_factory=list)
    citation_count: Optional[int] = None
    h_index: Optional[int] = None
    document_count: Optional[int] = None


@dataclass
class PPGInstance:
    """Programa de Pós-Graduação (para contexto relacional)."""
    id: str
    cd_programa_ies: str
    nm_programa_ies: str
    nm_area_conhecimento: str


# ============================================================================
# CLASSE 1: CAPESDocenteProcessor — Leitura e filtragem dos CSVs de docentes
# ============================================================================

class CAPESDocenteProcessor:
    """
    Lê os CSVs de docentes da CAPES e filtra para UFRPE + doutores.

    Uso:
        proc = CAPESDocenteProcessor()
        proc.read_csv_files()
        proc.filter_ufrpe_doutores()
        proc.normalize()
        proc.deduplicate_docentes()
    """

    # Colunas que esperamos encontrar nos CSVs de docentes CAPES.
    # Se o nome real da coluna for diferente no seu arquivo, ajuste aqui.
    COLUNAS_MAPEAMENTO = {
        "id_pessoa":    "ID_PESSOA",
        "nome":         "NM_DOCENTE",
        "titulacao_ano":"AN_TITULACAO",
        "titulacao_grau":"NM_GRAU_TITULACAO",
        "in_doutor":    "IN_DOUTOR",
        "categoria":    "DS_CATEGORIA_DOCENTE",
        "sigla_ies":    "SG_ENTIDADE_ENSINO",
        "cd_ies":       "CD_ENTIDADE_CAPES",
        "cd_programa":  "CD_PROGRAMA_IES",
        "nm_programa":  "NM_PROGRAMA_IES",
        "area":         "NM_AREA_CONHECIMENTO",
        "uf":           "SG_UF_PROGRAMA",
        "an_base":      "AN_BASE",
    }

    # Prioridade de categorias quando o docente aparece em múltiplos PPGs com
    # categorias diferentes — preferimos retratar o vínculo mais "forte".
    CATEGORIA_PRIORIDADE = {
        "PERMANENTE": 3,
        "COLABORADOR": 2,
        "VISITANTE": 1,
    }

    def __init__(self):
        self.dataframe: Optional[pd.DataFrame] = None
        self.canonical_df: Optional[pd.DataFrame] = None
        self.files_processed: List[str] = []
        DATA_PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    # ── Leitura ──────────────────────────────────────────────────────────────

    def read_csv_files(self) -> pd.DataFrame:
        """Lê e concatena todos os CSVs de docentes CAPES."""
        logger.info(f"Buscando CSVs de docentes em: {DATA_DOCENTES_DIR}")
        csv_files = sorted(DATA_DOCENTES_DIR.glob("br-capes-colsucup-docente*.csv"))

        if not csv_files:
            # Fallback: tentar qualquer CSV
            csv_files = sorted(DATA_DOCENTES_DIR.glob("*.csv"))

        if not csv_files:
            raise FileNotFoundError(
                f"Nenhum CSV encontrado em {DATA_DOCENTES_DIR}\n"
                f"Baixe os datasets 'Docentes da Pós-Graduação' do portal "
                f"de dados abertos da CAPES e coloque-os nessa pasta."
            )

        dfs = []
        for f in csv_files:
            logger.info(f"  Lendo {f.name}...")
            try:
                try:
                    df = pd.read_csv(
                        f,
                        delimiter=';',
                        encoding='iso-8859-1',
                        dtype={'AN_BASE': str},
                        low_memory=False,
                    )
                except (UnicodeDecodeError, Exception):
                    df = pd.read_csv(
                        f,
                        delimiter=';',
                        encoding='utf-8',
                        dtype={'AN_BASE': str},
                        low_memory=False,
                    )
                dfs.append(df)
                self.files_processed.append(f.name)
                logger.info(f"    ✓ {len(df):,} linhas")
            except Exception as e:
                logger.error(f"    ✗ Erro ao ler {f.name}: {e}")

        self.dataframe = pd.concat(dfs, ignore_index=True)
        logger.info(f"Total concatenado: {len(self.dataframe):,} linhas")

        # Verificar se as colunas esperadas existem
        self._verificar_colunas()
        return self.dataframe

    def _verificar_colunas(self):
        """Verifica se as colunas necessárias estão presentes no DataFrame."""
        colunas_necessarias = list(self.COLUNAS_MAPEAMENTO.values())
        colunas_presentes = set(self.dataframe.columns)
        faltantes = [c for c in colunas_necessarias if c not in colunas_presentes]

        if faltantes:
            logger.warning(
                f"⚠️  Colunas não encontradas: {faltantes}\n"
                f"    Colunas disponíveis: {sorted(colunas_presentes)}\n"
                f"    Ajuste COLUNAS_MAPEAMENTO se os nomes forem diferentes."
            )

    # ── Filtragem ────────────────────────────────────────────────────────────

    def filter_ufrpe_doutores(self) -> pd.DataFrame:
        """
        Filtra para manter apenas docentes:
          1. Vinculados à UFRPE (SG_ENTIDADE_ENSINO)
          2. Com titulação de DOUTORADO
          3. Com categoria PERMANENTE ou COLABORADOR

        Ajuste as constantes FILTRO_* no topo do script se necessário.
        """
        df = self.dataframe
        n0 = len(df)

        col_sigla = self.COLUNAS_MAPEAMENTO["sigla_ies"]
        col_cat = self.COLUNAS_MAPEAMENTO["categoria"]

        # 1) Instituição
        if col_sigla in df.columns:
            df = df[df[col_sigla].str.strip().str.upper() == FILTRO_SIGLA_IES]
            logger.info(f"  Filtro IES={FILTRO_SIGLA_IES}: {n0:,} → {len(df):,}")
        else:
            logger.warning(f"  Coluna {col_sigla} ausente — pulando filtro de IES")

        # 2) Apenas doutores (usa flag IN_DOUTOR = "SIM" ou NM_GRAU_TITULACAO)
        col_doutor = self.COLUNAS_MAPEAMENTO["in_doutor"]
        col_grau = self.COLUNAS_MAPEAMENTO["titulacao_grau"]
        if col_doutor in df.columns:
            df = df[df[col_doutor].str.strip().str.upper().isin(["S", "SIM", "1"])]
            logger.info(f"  Filtro IN_DOUTOR=SIM: → {len(df):,}")
        elif col_grau in df.columns:
            df = df[df[col_grau].str.strip().str.upper() == FILTRO_TIPO_TITULACAO]
            logger.info(f"  Filtro grau={FILTRO_TIPO_TITULACAO}: → {len(df):,}")
        else:
            logger.warning(f"  Colunas {col_doutor}/{col_grau} ausentes — pulando filtro de titulação")

        # 3) Categoria
        if col_cat in df.columns and FILTRO_CATEGORIAS:
            cats = [c.upper() for c in FILTRO_CATEGORIAS]
            df = df[df[col_cat].str.strip().str.upper().isin(cats)]
            logger.info(f"  Filtro categorias={cats}: → {len(df):,}")

        self.dataframe = df.reset_index(drop=True)
        return self.dataframe

    # ── Normalização ─────────────────────────────────────────────────────────

    def normalize(self):
        """Strip em strings, converte ano para numérico."""
        for col in self.dataframe.select_dtypes(include=['object']).columns:
            self.dataframe[col] = self.dataframe[col].str.strip()

        col_ano_tit = self.COLUNAS_MAPEAMENTO["titulacao_ano"]
        if col_ano_tit in self.dataframe.columns:
            self.dataframe[col_ano_tit] = pd.to_numeric(
                self.dataframe[col_ano_tit], errors='coerce'
            ).astype('Int64')

        col_an_base = self.COLUNAS_MAPEAMENTO["an_base"]
        if col_an_base in self.dataframe.columns:
            self.dataframe[col_an_base] = pd.to_numeric(
                self.dataframe[col_an_base], errors='coerce'
            ).astype('Int64')

        logger.info("Normalização concluída ✓")

    # ── Agregação ────────────────────────────────────────────────────────────

    def aggregate_pairs(self) -> pd.DataFrame:
        """
        Mantém uma linha por par (ID_PESSOA, CD_PROGRAMA_IES), com a entrada
        mais recente (AN_BASE mais alto). Preserva múltiplos vínculos
        docente↔PPG.
        """
        col_id = self.COLUNAS_MAPEAMENTO["id_pessoa"]
        col_prog = self.COLUNAS_MAPEAMENTO["cd_programa"]
        col_base = self.COLUNAS_MAPEAMENTO["an_base"]

        n_antes = len(self.dataframe)
        df = self.dataframe.dropna(subset=[col_id, col_prog])
        if col_base in df.columns:
            df = df.sort_values(col_base, ascending=False)
        df = df.drop_duplicates(subset=[col_id, col_prog], keep='first').reset_index(drop=True)
        self.dataframe = df

        logger.info(
            f"Pares únicos (ID_PESSOA, CD_PROGRAMA_IES): "
            f"{n_antes:,} → {len(df):,}"
        )
        return df

    def build_canonical_docentes(self) -> pd.DataFrame:
        """
        Reduz para uma linha por docente (chave: ID_PESSOA), agregando:
          - NM_DOCENTE: nome mais recente
          - AN_TITULACAO: ano mais antigo (doutorado original)
          - DS_CATEGORIA_DOCENTE: categoria de maior prioridade
                (PERMANENTE > COLABORADOR > VISITANTE)
        """
        col_id = self.COLUNAS_MAPEAMENTO["id_pessoa"]
        col_nome = self.COLUNAS_MAPEAMENTO["nome"]
        col_titulacao = self.COLUNAS_MAPEAMENTO["titulacao_ano"]
        col_cat = self.COLUNAS_MAPEAMENTO["categoria"]
        col_base = self.COLUNAS_MAPEAMENTO["an_base"]

        df = self.dataframe.copy()
        df["_cat_prio"] = df[col_cat].map(self.CATEGORIA_PRIORIDADE).fillna(0)

        # Para cada ID_PESSOA, ordenar por (cat_prio desc, AN_BASE desc) e
        # pegar o NM_DOCENTE da entrada de maior prioridade.
        df_sorted = df.sort_values(
            ["_cat_prio", col_base], ascending=[False, False]
        )
        canonical = df_sorted.drop_duplicates(subset=[col_id], keep='first')

        # AN_TITULACAO mínimo (doutorado mais antigo) por ID
        an_min = (
            df.dropna(subset=[col_titulacao])
              .groupby(col_id)[col_titulacao].min()
        )

        canonical = canonical.set_index(col_id)
        canonical[col_titulacao] = an_min
        canonical = canonical.reset_index()

        cols = [col_id, col_nome, col_titulacao, col_cat]
        self.canonical_df = canonical[cols].reset_index(drop=True)
        logger.info(
            f"Docentes canônicos (únicos por ID_PESSOA): {len(self.canonical_df):,}"
        )
        return self.canonical_df

    # ── Exportação ───────────────────────────────────────────────────────────

    def save_processed(
        self,
        canonical_filename: str = "docentes_ufrpe.csv",
        pairs_filename: str = "docentes_ufrpe_vinculos.csv",
    ) -> Tuple[Path, Path]:
        out_can = DATA_PROCESSED_DIR / canonical_filename
        out_pairs = DATA_PROCESSED_DIR / pairs_filename

        if self.canonical_df is not None:
            self.canonical_df.to_csv(
                out_can, sep=';', encoding='utf-8-sig', index=False
            )
            logger.info(
                f"Canônico salvo: {out_can} ({len(self.canonical_df):,} docentes)"
            )

        col_id = self.COLUNAS_MAPEAMENTO["id_pessoa"]
        col_prog = self.COLUNAS_MAPEAMENTO["cd_programa"]
        col_nm_prog = self.COLUNAS_MAPEAMENTO["nm_programa"]
        col_area = self.COLUNAS_MAPEAMENTO["area"]
        cols_pairs = [c for c in [col_id, col_prog, col_nm_prog, col_area]
                      if c in self.dataframe.columns]
        self.dataframe[cols_pairs].to_csv(
            out_pairs, sep=';', encoding='utf-8-sig', index=False
        )
        logger.info(
            f"Vínculos salvos: {out_pairs} ({len(self.dataframe):,} pares)"
        )
        return out_can, out_pairs


# ============================================================================
# CLASSE 2: ScopusEnricher — Enriquecimento com dados do Scopus
# ============================================================================

class ScopusEnricher:
    """
    Cruza os docentes CAPES com dados de citação do Scopus.

    Espera um CSV em data/raw/scopus/scopus_ufrpe.csv com ao menos:
      - author_name   (nome do autor no Scopus)
      - citation_count
      - h_index        (opcional)
      - document_count (opcional)

    O cruzamento é feito por similaridade de nome (normalizado).
    """

    def __init__(self, scopus_path: Optional[Path] = None):
        self.scopus_path = scopus_path or (DATA_SCOPUS_DIR / "scopus_ufrpe.csv")
        self.scopus_df: Optional[pd.DataFrame] = None

    @staticmethod
    def _normalize_name(name: str) -> str:
        """Remove acentos, converte para minúsculas, remove pontuação."""
        if not isinstance(name, str):
            return ""
        name = unicodedata.normalize('NFKD', name)
        name = ''.join(c for c in name if not unicodedata.combining(c))
        name = name.lower().strip()
        name = re.sub(r'[^a-z\s]', '', name)
        name = re.sub(r'\s+', ' ', name)
        return name

    def load(self) -> bool:
        """Carrega o CSV do Scopus. Retorna False se não existir."""
        if not self.scopus_path.exists():
            logger.warning(
                f"⚠️  Arquivo Scopus não encontrado: {self.scopus_path}\n"
                f"    O pipeline continuará sem dados de citação.\n"
                f"    Para incluir citações, exporte os dados do Scopus e "
                f"salve nesse caminho."
            )
            return False

        self.scopus_df = pd.read_csv(self.scopus_path, encoding='utf-8')
        logger.info(
            f"Scopus carregado: {len(self.scopus_df):,} autores de "
            f"{self.scopus_path.name}"
        )
        return True

    def enrich(self, docentes_df: pd.DataFrame, col_nome: str) -> pd.DataFrame:
        """
        Enriquece o DataFrame de docentes com citation_count, h_index,
        document_count do Scopus.
        """
        if self.scopus_df is None:
            docentes_df['citation_count'] = None
            docentes_df['h_index'] = None
            docentes_df['document_count'] = None
            return docentes_df

        # Criar chave normalizada em ambos os lados
        docentes_df['_nome_norm'] = docentes_df[col_nome].apply(self._normalize_name)
        self.scopus_df['_nome_norm'] = self.scopus_df['author_name'].apply(self._normalize_name)

        # Merge por nome normalizado
        merged = docentes_df.merge(
            self.scopus_df[['_nome_norm', 'citation_count', 'h_index', 'document_count']].drop_duplicates('_nome_norm'),
            on='_nome_norm',
            how='left'
        )

        n_matched = merged['citation_count'].notna().sum()
        logger.info(
            f"Cruzamento Scopus: {n_matched}/{len(merged)} docentes "
            f"encontrados ({n_matched/len(merged)*100:.1f}%)"
        )

        merged.drop(columns=['_nome_norm'], inplace=True)
        return merged


# ============================================================================
# CLASSE 3: InstanceExtractor — Extrai instâncias para a ontologia
# ============================================================================

class DocenteInstanceExtractor:
    """Extrai DocenteInstance e PPGInstance a partir dos DataFrames processados."""

    def __init__(
        self,
        canonical_df: pd.DataFrame,
        pairs_df: pd.DataFrame,
        col_map: dict,
    ):
        # canonical_df: 1 linha por ID_PESSOA (com métricas Scopus mescladas).
        # pairs_df:     1 linha por par (ID_PESSOA, CD_PROGRAMA_IES) — a partir
        #               dele extraímos PPGs e os vínculos.
        self.canonical_df = canonical_df
        self.pairs_df = pairs_df
        self.col = col_map
        self.docente_instances: Dict[str, DocenteInstance] = {}
        self.ppg_instances: Dict[str, PPGInstance] = {}

    @staticmethod
    def _docente_id(id_pessoa) -> str:
        return f"docente_{int(id_pessoa)}"

    @staticmethod
    def _ppg_id(cd_programa) -> str:
        return f"ppg_{str(cd_programa).strip()}"

    def extract_docentes(self) -> Dict[str, DocenteInstance]:
        logger.info("Extraindo instâncias de Docente...")

        col_id = self.col["id_pessoa"]

        # Vínculos por docente (lista de ppg_ids), construída a partir do pairs_df
        vinculos_por_docente: Dict[str, List[str]] = defaultdict(list)
        for _, row in self.pairs_df.iterrows():
            doc_id = self._docente_id(row[col_id])
            ppg_id = self._ppg_id(row[self.col["cd_programa"]])
            if ppg_id not in vinculos_por_docente[doc_id]:
                vinculos_por_docente[doc_id].append(ppg_id)

        for _, row in self.canonical_df.iterrows():
            doc_id = self._docente_id(row[col_id])
            an_tit = row.get(self.col["titulacao_ano"])
            an_tit = int(an_tit) if pd.notna(an_tit) else 0

            self.docente_instances[doc_id] = DocenteInstance(
                id=doc_id,
                nm_docente=str(row.get(self.col["nome"], "")),
                an_titulacao=an_tit,
                ds_categoria=str(row.get(self.col["categoria"], "")),
                vinculados_a=sorted(vinculos_por_docente.get(doc_id, [])),
                citation_count=(
                    int(row['citation_count'])
                    if pd.notna(row.get('citation_count')) else None
                ),
                h_index=(
                    int(row['h_index'])
                    if pd.notna(row.get('h_index')) else None
                ),
                document_count=(
                    int(row['document_count'])
                    if pd.notna(row.get('document_count')) else None
                ),
            )

        n_vinc = sum(len(d.vinculados_a) for d in self.docente_instances.values())
        logger.info(
            f"  {len(self.docente_instances)} docentes extraídos "
            f"({n_vinc} vínculos a PPGs)"
        )
        return self.docente_instances

    def extract_ppgs(self) -> Dict[str, PPGInstance]:
        logger.info("Extraindo instâncias de PPG...")

        col_cd = self.col["cd_programa"]
        col_nm = self.col["nm_programa"]
        col_area = self.col["area"]

        if col_cd not in self.pairs_df.columns:
            logger.warning(f"  Coluna {col_cd} ausente — PPGs não extraídos")
            return self.ppg_instances

        unique = self.pairs_df.drop_duplicates(subset=[col_cd], keep='first')
        for _, row in unique.iterrows():
            ppg_id = self._ppg_id(row[col_cd])
            self.ppg_instances[ppg_id] = PPGInstance(
                id=ppg_id,
                cd_programa_ies=str(row[col_cd]),
                nm_programa_ies=str(row.get(col_nm, "")),
                nm_area_conhecimento=str(row.get(col_area, "")),
            )

        logger.info(f"  {len(self.ppg_instances)} PPGs extraídos")
        return self.ppg_instances

    def get_summary(self) -> Dict:
        with_citations = sum(
            1 for d in self.docente_instances.values()
            if d.citation_count is not None
        )
        return {
            'docentes': len(self.docente_instances),
            'ppgs': len(self.ppg_instances),
            'com_citacoes': with_citations,
        }


# ============================================================================
# CLASSE 4: OMLGenerator — Gera o arquivo OML
# ============================================================================

class OMLDocenteGenerator:
    """Gera o arquivo OML com instâncias de Docente e PPG."""

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _esc(value: str) -> str:
        return str(value).replace('"', '\\"')

    def generate(
        self,
        docentes: Dict[str, DocenteInstance],
        ppgs: Dict[str, PPGInstance],
    ) -> str:
        lines = [
            f'description <{DESCRIPTION_URI}#> as docentes-ufrpe {{',
            '',
            f'\tuses <{VOCABULARY_URI}#> as {VOCABULARY_NS}',
            '',
            '\t// =================================================================',
            '\t// PROGRAMAS DE PÓS-GRADUAÇÃO (PPG)',
            '\t// =================================================================',
        ]

        for ppg in ppgs.values():
            lines.extend([
                '',
                f'\tinstance {ppg.id} : {VOCABULARY_NS}:PPG [',
                f'\t\t{VOCABULARY_NS}:cd_programa_ies "{ppg.cd_programa_ies}"',
                f'\t\t{VOCABULARY_NS}:nm_programa_ies "{self._esc(ppg.nm_programa_ies)}"',
                f'\t\t{VOCABULARY_NS}:nm_area_conhecimento "{self._esc(ppg.nm_area_conhecimento)}"',
                '\t]',
            ])

        lines.extend([
            '',
            '\t// =================================================================',
            '\t// DOCENTES',
            '\t// =================================================================',
        ])

        for doc in docentes.values():
            lines.append('')
            lines.append(f'\tinstance {doc.id} : {VOCABULARY_NS}:Docente [')
            lines.append(f'\t\t{VOCABULARY_NS}:nm_docente "{self._esc(doc.nm_docente)}"')
            lines.append(f'\t\t{VOCABULARY_NS}:an_titulacao {doc.an_titulacao}')
            lines.append(f'\t\t{VOCABULARY_NS}:ds_categoria "{doc.ds_categoria}"')

            if doc.citation_count is not None:
                lines.append(f'\t\t{VOCABULARY_NS}:citation_count {doc.citation_count}')
            if doc.h_index is not None:
                lines.append(f'\t\t{VOCABULARY_NS}:h_index {doc.h_index}')
            if doc.document_count is not None:
                lines.append(f'\t\t{VOCABULARY_NS}:document_count {doc.document_count}')

            for ppg_id in doc.vinculados_a:
                lines.append(f'\t\t{VOCABULARY_NS}:vinculado_a {ppg_id}')

            lines.append('\t]')

        lines.extend(['', '}'])
        return '\n'.join(lines)

    def save(self, filename: str, content: str) -> Path:
        path = self.output_dir / filename
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        logger.info(f"OML salvo: {path}")
        return path


# ============================================================================
# MAIN
# ============================================================================

def print_summary(summary: Dict):
    print("\n" + "=" * 60)
    print(" RESUMO DA EXTRAÇÃO")
    print("=" * 60)
    print(f"  Docentes únicos:              {summary['docentes']:,}")
    print(f"  PPGs:                         {summary['ppgs']:,}")
    print(f"  Docentes com citações Scopus: {summary['com_citacoes']:,}")
    print("=" * 60 + "\n")


def main() -> int:
    logger.info("=" * 60)
    logger.info(" Pipeline: Docentes UFRPE → OML")
    logger.info("=" * 60)

    try:
        # ── ETAPA 1: Ler CSVs de docentes ────────────────────────────
        logger.info("\n[1/5] Lendo CSVs de docentes CAPES...")
        proc = CAPESDocenteProcessor()
        proc.read_csv_files()

        # ── ETAPA 2: Filtrar UFRPE + Doutores e agregar pares ─────────
        logger.info("\n[2/5] Filtrando UFRPE + Doutores...")
        proc.filter_ufrpe_doutores()
        proc.normalize()
        proc.aggregate_pairs()
        proc.build_canonical_docentes()

        # ── ETAPA 3: Enriquecer canônico com Scopus ───────────────────
        logger.info("\n[3/5] Enriquecendo com dados Scopus...")
        enricher = ScopusEnricher()
        has_scopus = enricher.load()
        if has_scopus:
            proc.canonical_df = enricher.enrich(
                proc.canonical_df,
                proc.COLUNAS_MAPEAMENTO["nome"]
            )
        else:
            for col in ['citation_count', 'h_index', 'document_count']:
                proc.canonical_df[col] = None

        # ── ETAPA 4: Extrair instâncias ───────────────────────────────
        logger.info("\n[4/5] Extraindo instâncias...")
        extractor = DocenteInstanceExtractor(
            proc.canonical_df,
            proc.dataframe,  # pares (ID_PESSOA, CD_PROGRAMA_IES)
            proc.COLUNAS_MAPEAMENTO,
        )
        extractor.extract_docentes()
        extractor.extract_ppgs()
        print_summary(extractor.get_summary())

        # ── ETAPA 5: Gerar OML ────────────────────────────────────────
        logger.info("[5/5] Gerando arquivo OML...")
        gen = OMLDocenteGenerator(OML_OUTPUT_DIR)
        content = gen.generate(
            extractor.docente_instances,
            extractor.ppg_instances,
        )
        gen.save("docentes-ufrpe.oml", content)

        # Salvar CSV processado
        proc.save_processed()

        logger.info("\n" + "=" * 60)
        logger.info(" PIPELINE CONCLUÍDO COM SUCESSO!")
        logger.info("=" * 60)
        return 0

    except Exception as e:
        logger.error(f"\n✗ Erro: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
