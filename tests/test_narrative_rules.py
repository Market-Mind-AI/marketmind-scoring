"""As regras de linguagem da narrativa valem nos três idiomas em que ela sai."""

from app.services.narrative_rules import language_violations


def test_each_language_has_its_own_rules() -> None:
    assert language_violations("This is proven demand.", "en")
    assert language_violations("Há uma demanda comprovada no Brasil e o retorno é garantido.", "pt")
    assert len(language_violations("Hay demanda comprobada; el éxito está garantizado.", "es")) == 2


def test_the_rules_are_the_analysis_languages() -> None:
    """Uma narrativa em português lida com as regras em inglês passava inteira."""
    assert not language_violations("Há uma demanda comprovada.", "en")
    assert not language_violations("O interesse de busca é alto, o que indica oportunidade.", "pt")
