"""O idioma de uma análise: um lugar só para decidir qual é, e qual vale
quando ninguém disse.

O idioma da interface de quem pediu viaja com a análise
(`analysis_requests.language`), e é nele que sai tudo o que é lido: a
narrativa, os rótulos dos sinais e das dimensões, as faixas, o glossário e as
frases por score. Antes cada parte tinha a sua ideia de padrão — a narrativa
lia o configurado, a nota de confiança da demanda saía sempre em português e
os rótulos sempre em inglês —, e um relatório em espanhol misturava os três.
"""

from app.core.config import settings

# Os idiomas que têm textos próprios. Um idioma fora destes ainda recebe a
# narrativa, pedida em inglês com uma linha dizendo o idioma; o resto do
# relatório sai no padrão.
SUPPORTED_LANGUAGES: tuple[str, ...] = ("pt", "en", "es")

_ALIASES: dict[str, tuple[str, ...]] = {
    "pt": ("pt", "pt-br", "pt_br", "portuguese", "portugues", "português"),
    "en": ("en", "en-us", "en_us", "english"),
    "es": ("es", "es-es", "es_es", "es-419", "spanish", "espanol", "español", "castellano"),
}


def language_key(language: str | None) -> str | None:
    """'pt', 'en' or 'es' when the language has its own texts; None otherwise."""
    lang = (language or "").strip().lower()
    for key, aliases in _ALIASES.items():
        if lang in aliases:
            return key
    return None


def requested_language(language: str | None) -> str:
    """O idioma que a análise pediu, como pedido, ou o padrão configurado."""
    return (language or "").strip() or settings.ollama_llm_narrative_language


def analysis_language(language: str | None) -> str:
    """O idioma em que o relatório desta análise sai: um dos que têm textos
    próprios. O pedido quando é um deles, senão o padrão configurado, senão
    inglês."""
    return language_key(language) or language_key(settings.ollama_llm_narrative_language) or "en"
