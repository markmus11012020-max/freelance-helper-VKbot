from __future__ import annotations

from app.config import Settings
from app.parsers.base import BaseParser, HttpClient
from app.parsers.fl_ru import FlRuParser
from app.parsers.freelance_ru import FreelanceRuParser
from app.parsers.kwork import KworkParser


def build_parsers(settings: Settings, http: HttpClient) -> list[BaseParser]:
    parsers: list[BaseParser] = []
    if settings.fl_ru_enabled:
        parsers.append(FlRuParser(settings, http))
    if settings.freelance_ru_enabled:
        parsers.append(FreelanceRuParser(settings, http))
    if settings.kwork_enabled:
        parsers.append(KworkParser(settings, http))
    return parsers


__all__ = [
    "BaseParser",
    "HttpClient",
    "FlRuParser",
    "FreelanceRuParser",
    "KworkParser",
    "build_parsers",
]
