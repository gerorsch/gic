from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent

load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(SCRIPT_DIR / ".env")


AUTHOR_SEARCH_URL = "https://api.elsevier.com/content/search/author"
AUTHOR_RETRIEVAL_URL = "https://api.elsevier.com/content/author/author_id"


class ScopusAPIError(RuntimeError):
    """Represents an error returned by the Elsevier API."""


@dataclass
class AuthorCandidate:
    author_id: str
    indexed_name: str | None = None
    given_name: str | None = None
    surname: str | None = None
    affiliation_name: str | None = None
    document_count: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class AuthorMetrics:
    author_id: str
    citation_count: int | None
    h_index: int | None
    document_count: int | None
    indexed_name: str | None = None
    affiliation_name: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _strip_prefix(value: str | None, prefix: str) -> str:
    if not value:
        return ""
    if value.startswith(prefix):
        return value[len(prefix):]
    return value


def _nested_get(payload: dict[str, Any], path: list[str]) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


class ScopusClient:
    def __init__(
        self,
        api_key: str | None = None,
        insttoken: str | None = None,
        timeout_seconds: int = 40,
    ) -> None:
        self.api_key = api_key or os.getenv("ELSEVIER_API_KEY")
        self.insttoken = insttoken or os.getenv("ELSEVIER_INSTTOKEN")
        self.timeout_seconds = timeout_seconds

        if not self.api_key:
            raise ValueError(
                "ELSEVIER_API_KEY nao encontrado. Defina no .env (raiz ou scripts/api-scopus/.env)."
            )

    def _headers(self) -> dict[str, str]:
        headers = {
            "X-ELS-APIKey": self.api_key,
            "Accept": "application/json",
        }
        if self.insttoken:
            headers["X-ELS-Insttoken"] = self.insttoken
        return headers

    def _get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = requests.get(
            url,
            headers=self._headers(),
            params=params or {},
            timeout=self.timeout_seconds,
        )

        if response.status_code != 200:
            message = response.text[:1000]
            raise ScopusAPIError(
                f"Erro na API Elsevier ({response.status_code}) em {url}: {message}"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise ScopusAPIError(f"Resposta da API nao esta em JSON valido: {exc}") from exc

    def search_authors(self, query: str, count: int = 25, start: int = 0, view: str = "STANDARD") -> list[AuthorCandidate]:
        data = self._get(
            AUTHOR_SEARCH_URL,
            params={
                "query": query,
                "count": min(count, 25 if view.upper() == "COMPLETE" else 200),
                "start": start,
                "view": view,
            },
        )

        entries = _as_list(_nested_get(data, ["search-results", "entry"]))
        return [self._parse_author_candidate(entry) for entry in entries if isinstance(entry, dict)]

    def search_author_by_name(
        self,
        first_name: str,
        last_name: str,
        affiliation_hint: str | None = None,
        count: int = 25,
        view: str = "STANDARD",
    ) -> tuple[str, list[AuthorCandidate]]:
        parts = [f"AUTHLAST({last_name})", f"AUTHFIRST({first_name})"]
        if affiliation_hint:
            parts.append(f'AFFIL("{affiliation_hint}")')
        query = " AND ".join(parts)

        candidates = self.search_authors(query=query, count=count, start=0, view=view)
        if candidates or not affiliation_hint:
            return query, candidates

        fallback_query = " AND ".join(parts[:2])
        return fallback_query, self.search_authors(
            query=fallback_query,
            count=count,
            start=0,
            view=view,
        )

    def get_author_metrics(self, author_id: str, view: str = "METRICS") -> AuthorMetrics:
        clean_id = _strip_prefix(author_id, "AUTHOR_ID:")
        if not clean_id:
            raise ValueError("author_id invalido para retrieval.")

        data = self._get(
            f"{AUTHOR_RETRIEVAL_URL}/{clean_id}",
            params={"view": view},
        )

        payload = _nested_get(data, ["author-retrieval-response"])
        if isinstance(payload, list):
            payload = payload[0] if payload else {}
        if not isinstance(payload, dict):
            payload = {}

        core = payload.get("coredata", {}) if isinstance(payload.get("coredata"), dict) else {}
        author_profile = payload.get("author-profile", {}) if isinstance(payload.get("author-profile"), dict) else {}
        preferred_name = author_profile.get("preferred-name", {}) if isinstance(author_profile.get("preferred-name"), dict) else {}

        resolved_author_id = _strip_prefix(core.get("dc:identifier"), "AUTHOR_ID:") or clean_id

        return AuthorMetrics(
            author_id=resolved_author_id,
            citation_count=_safe_int(core.get("citation-count") or payload.get("citation-count")),
            h_index=_safe_int(payload.get("h-index") or core.get("h-index")),
            document_count=_safe_int(core.get("document-count") or payload.get("document-count")),
            indexed_name=preferred_name.get("indexed-name") or _nested_get(payload, ["preferred-name", "indexed-name"]),
            affiliation_name=self._extract_affiliation_name(payload),
            raw=payload,
        )

    @staticmethod
    def _parse_author_candidate(entry: dict[str, Any]) -> AuthorCandidate:
        preferred_name = entry.get("preferred-name", {})
        if not isinstance(preferred_name, dict):
            preferred_name = {}

        affiliation_current = entry.get("affiliation-current", {})
        if isinstance(affiliation_current, list):
            affiliation_current = affiliation_current[0] if affiliation_current else {}
        if not isinstance(affiliation_current, dict):
            affiliation_current = {}

        return AuthorCandidate(
            author_id=_strip_prefix(entry.get("dc:identifier"), "AUTHOR_ID:"),
            indexed_name=preferred_name.get("indexed-name"),
            given_name=preferred_name.get("given-name"),
            surname=preferred_name.get("surname"),
            affiliation_name=affiliation_current.get("affiliation-name"),
            document_count=_safe_int(entry.get("document-count")),
            raw=entry,
        )

    @staticmethod
    def _extract_affiliation_name(author_payload: dict[str, Any]) -> str | None:
        current = author_payload.get("affiliation-current")
        if isinstance(current, dict):
            current_aff = current.get("affiliation")
            if isinstance(current_aff, list):
                current_aff = current_aff[0] if current_aff else {}
            if isinstance(current_aff, dict):
                return (
                    current_aff.get("affiliation-name")
                    or current_aff.get("preferred-name")
                    or current_aff.get("affiliation-name-display")
                )
            return current.get("affiliation-name")

        profile = author_payload.get("author-profile", {})
        if isinstance(profile, dict):
            prof_cur = profile.get("affiliation-current", {})
            if isinstance(prof_cur, dict):
                prof_aff = prof_cur.get("affiliation")
                if isinstance(prof_aff, list):
                    prof_aff = prof_aff[0] if prof_aff else {}
                if isinstance(prof_aff, dict):
                    return prof_aff.get("preferred-name") or prof_aff.get("affiliation-name")

        return None
