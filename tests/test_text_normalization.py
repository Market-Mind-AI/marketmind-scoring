from app.utils.text import as_term_list, normalize_alias


def test_lowercases_and_collapses_whitespace() -> None:
    assert normalize_alias("  Air   Fryer  ") == "air fryer"


def test_strips_accents() -> None:
    assert normalize_alias("Fritadeira sem Óleo") == "fritadeira sem oleo"


def test_strips_punctuation() -> None:
    assert normalize_alias("Air-Fryer, XL!") == "air fryer xl"


def test_same_input_always_normalizes_the_same() -> None:
    assert normalize_alias("Air Fryer") == normalize_alias("  air   FRYER ")


def test_preserva_escrita_nao_latina() -> None:
    """Antes de 2026-08-25 tudo isto virava string VAZIA, e o vazio era gravado
    como alias — todo texto não-latino passava a casar com o primeiro que
    tivesse sido cadastrado. Ver app/utils/text.normalize_alias."""
    assert normalize_alias("ノンフライヤー") == "ノンフライヤー"
    assert normalize_alias("空气炸锅") == "空气炸锅"
    assert normalize_alias("에어프라이어") == "에어프라이어"  # NFC recompõe os jamo que o NFKD separou
    assert normalize_alias("Пылесос-робот") == "пылесос робот"
    # O que realmente importa: escritas diferentes não colapsam no mesmo valor.
    assert normalize_alias("ノンフライヤー") != normalize_alias("空气炸锅")


def test_normalizacao_e_idempotente() -> None:
    """Normalizar o já normalizado tem que devolver o mesmo — senão o alias
    gravado não casaria com a próxima consulta idêntica."""
    for texto in ("Air Fryer", "pão de queijo", "Heißluftfritteuse", "에어프라이어", "Пылесос-робот"):
        uma_vez = normalize_alias(texto)
        assert normalize_alias(uma_vez) == uma_vez


def test_preserva_letras_latinas_sem_decomposicao() -> None:
    """`ß` e `ł` não têm decomposição NFKD, então a regra antiga os trocava por
    espaço e perdia informação no meio da palavra."""
    assert normalize_alias("Heißluftfritteuse") == "heißluftfritteuse"
    assert normalize_alias("odżywka bez spłukiwania") == "odzywka bez spłukiwania"


def test_texto_sem_letra_nem_digito_normaliza_para_vazio() -> None:
    """Continua vazio de propósito — o que mudou é que ninguém grava isso como
    alias (catalog_service._gravar_alias + CHECK da migration 028)."""
    assert normalize_alias("!!!") == ""
    assert normalize_alias("😀") == ""
    assert normalize_alias("   ") == ""


def test_as_term_list_tolerates_legacy_string_and_list() -> None:
    assert as_term_list("presunto pata negra") == ["presunto pata negra"]
    assert as_term_list(["a", "b"]) == ["a", "b"]
    assert as_term_list(None) == []
    assert as_term_list(["", "  ", "x"]) == ["x"]  # blanks dropped
