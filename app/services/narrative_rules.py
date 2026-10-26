"""As frases que a narrativa não pode usar, nos três idiomas em que ela sai.

São regras do método, não juízo: o que as fontes medem é gente BUSCANDO, não
gente comprando, e a nota é uma leitura de oportunidade, nunca uma garantia.
Moravam no orquestrador e só em inglês, enquanto a narrativa sai em português,
espanhol ou inglês — uma "demanda comprovada" passava sem ninguém ver. Ficam
aqui, ao lado dos textos da narrativa, e o orquestrador pergunta por
POST /internal/narrative/language-check.
"""

import re
from dataclasses import dataclass

from app.utils.language import analysis_language


@dataclass(frozen=True)
class LanguageRule:
    pattern: re.Pattern[str]
    # Por que a frase é proibida, em inglês: é para quem lê o trace.
    reason: str


_DEMANDA = "search interest is not proven demand — the sources measure people searching, not people buying"
_GARANTIA = "the score is an opportunity reading, never a guarantee"

RULES: dict[str, list[LanguageRule]] = {
    "en": [
        LanguageRule(re.compile(r"\b(proven|confirmed|actual|real)\s+demand\b", re.I), _DEMANDA),
        LanguageRule(re.compile(r"\bguarantee(s|d)?\b", re.I), _GARANTIA),
    ],
    "pt": [
        LanguageRule(re.compile(r"\bdemanda\s+(comprovada|confirmada|real|garantida)\b", re.I), _DEMANDA),
        LanguageRule(re.compile(r"\bgarant(e|em|ia|ias|ido|ida|idos|idas|ir|imos)\b", re.I), _GARANTIA),
    ],
    "es": [
        LanguageRule(re.compile(r"\bdemanda\s+(comprobada|probada|confirmada|real|garantizada)\b", re.I), _DEMANDA),
        LanguageRule(re.compile(r"\bgarant(ía|ías|ia|iza|izan|izado|izada|izados|izadas|izar)\b", re.I), _GARANTIA),
    ],
}


def language_violations(narrative: str, language: str | None) -> list[str]:
    """Cada frase proibida que a narrativa usa, com o motivo. As regras são as
    do idioma da análise, que é o idioma em que a narrativa foi pedida."""
    violacoes: list[str] = []
    for regra in RULES[analysis_language(language)]:
        for achado in regra.pattern.finditer(narrative or ""):
            violacoes.append(f'"{achado.group(0)}": {regra.reason}')
    return violacoes
