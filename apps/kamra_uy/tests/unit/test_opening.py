"""No vender antes de la apertura (HR-263): la regla pura, el cableado de hooks.py y el texto de la web (sin frappe).

La llegada anterior a la fecha de apertura de la propiedad se rechaza; la del mismo día o posterior, no; sin fecha de
apertura (el campo vacío), nada. El motivo del rechazo es la misma línea que la web pinta en el paso 1
(`fechas.apertura` de web/content/{es,pt,en}/reservar.md): esta prueba los ata, porque el contenedor de Kamra no
tiene web/ y la guarda no puede vivir allí.
"""

from __future__ import annotations

import ast
import re
from datetime import date, datetime
from pathlib import Path

import pytest

from kamra_uy.pure import opening

APP_ROOT = Path(__file__).resolve().parents[2]
REPO = APP_ROOT.parents[1]
OPENING = date(2026, 12, 26)


def _hooks() -> dict[str, object]:
    tree = ast.parse((APP_ROOT / "kamra_uy" / "hooks.py").read_text())
    return {node.targets[0].id: ast.literal_eval(node.value) for node in tree.body
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)}


# --- la fecha ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("valor, esperado", [
    (None, None), ("", None), ("   ", None),
    ("2026-12-26", OPENING), (" 2026-12-26 ", OPENING), ("2026-12-26 00:00:00", OPENING),
    (OPENING, OPENING), (datetime(2026, 12, 26, 15, 30), OPENING),
    ("2026-02-30", None), ("26/12/2026", None), ("mañana", None), (20261226, None),
])
def test_fecha(valor, esperado) -> None:
    assert opening.fecha(valor) == esperado


# --- la regla ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("llegada", ["2026-12-25", "2026-12-20", "2026-11-21", date(2026, 12, 25)])
def test_llegada_anterior_a_la_apertura(llegada) -> None:
    assert opening.antes_de_abrir(llegada, OPENING)
    assert opening.antes_de_abrir(llegada, "2026-12-26")


@pytest.mark.parametrize("llegada", ["2026-12-26", "2026-12-27", "2027-01-08", OPENING])
def test_llegada_el_dia_de_la_apertura_o_despues(llegada) -> None:
    assert not opening.antes_de_abrir(llegada, OPENING)


@pytest.mark.parametrize("apertura", [None, "", "no es una fecha"])
def test_sin_apertura_no_hay_restriccion(apertura) -> None:
    assert not opening.antes_de_abrir("2000-01-01", apertura)
    assert not opening.antes_de_abrir("2026-12-25", apertura)


@pytest.mark.parametrize("llegada", [None, "", "2026-13-01", "ayer"])
def test_una_llegada_que_no_es_fecha_la_rechaza_kamra(llegada) -> None:
    """La regla no la juzga: la búsqueda o la reserva de Kamra la rechazan con su propio motivo."""
    assert not opening.antes_de_abrir(llegada, OPENING)


# --- el motivo, en el idioma de la página ------------------------------------------------------------------------

def test_motivo_en_los_tres_idiomas() -> None:
    assert opening.motivo(OPENING, "es") == (
        "Apertura del hotel: Sáb 26 dic 2026. Puedes reservar estadías con llegada desde esa fecha.")
    assert opening.motivo(OPENING, "pt") == (
        "Abertura do hotel: Sáb 26 dez 2026. Você pode reservar estadias com chegada a partir dessa data.")
    assert opening.motivo(OPENING, "en") == (
        "Hotel opening: Sat 26 Dec 2026. You can book stays arriving from that date.")


@pytest.mark.parametrize("idioma", [None, "", "fr", "ES"])
def test_motivo_en_otro_idioma_sale_en_espanol(idioma) -> None:
    assert opening.motivo("2026-12-26", idioma) == opening.motivo(OPENING, "es")


def test_motivo_sin_fecha_es_un_error() -> None:
    with pytest.raises(ValueError):
        opening.motivo(None, "es")


@pytest.mark.parametrize("idioma", ["es", "pt", "en"])
def test_el_motivo_es_la_linea_de_la_web(idioma) -> None:
    """`fechas.apertura` de reservar.md, con el mismo hueco {fecha}: si el fundador la cambia allí, cambia aquí."""
    texto = (REPO / "web" / "content" / idioma / "reservar.md").read_text(encoding="utf-8")
    linea = re.search(r"^- fechas\.apertura: (.+)$", texto, re.M)
    assert linea, f"falta fechas.apertura en web/content/{idioma}/reservar.md"
    assert opening.TEXTOS[idioma] == linea.group(1)
    assert '"fechas.apertura"' in texto.split("\n---\n", 1)[0], "es un borrador (D-HR-44) hasta el OK del fundador"


def test_textos_sin_voseo_y_con_voce() -> None:
    assert not re.search(r"\b(podés|reservá|vos)\b", opening.TEXTOS["es"])
    assert "Você" in opening.TEXTOS["pt"]


# --- el cableado (hooks.py) --------------------------------------------------------------------------------------

def test_el_campo_se_crea_al_instalar_y_en_cada_migrate() -> None:
    hooks = _hooks()
    for hook in ("after_install", "after_migrate"):
        assert "kamra_uy.booking.opening.ensure_fields" in hooks[hook], hook
        assert "kamra_uy.notifications.confirmation.ensure_fields" in hooks[hook], hook


def test_la_busqueda_la_reserva_y_la_vitrina_pasan_por_el_guardian() -> None:
    assert _hooks()["override_whitelisted_methods"] == {
        "kamra.public_api.showcase": "kamra_uy.booking.opening.showcase",
        "kamra.public_api.search_stay": "kamra_uy.booking.opening.search_stay",
        "kamra.public_api.book": "kamra_uy.booking.opening.book",
    }


def test_toda_reserva_web_pasa_por_el_guardian_al_crearse() -> None:
    before_insert = _hooks()["doc_events"]["Reservation"]["before_insert"]
    # el guardián primero: una reserva que no se va a crear no apunta nada
    assert before_insert == ["kamra_uy.booking.opening.on_reservation_before_insert",
                             "kamra_uy.notifications.confirmation.remember_language"]
