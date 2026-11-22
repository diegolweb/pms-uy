"""Las plataformas tampoco venden antes de la apertura (HR-264): la regla pura del cierre de venta y su cableado (sin
frappe).

Con la fecha de apertura de la propiedad en el futuro, kamra_uy mantiene UNA Channel Restriction stop sell de la
propiedad (todos los tipos y planes) de hoy a la víspera de la apertura; el outbox de ARI de Kamra la manda a Channex.
Sin fecha, o con la apertura hoy o ya pasada, no hay cierre. La fila es de kamra_uy por su nombre (determinista, con
un prefijo propio): una restricción puesta a mano tiene el nombre al azar de Kamra y nunca se toca.
"""

from __future__ import annotations

import ast
from datetime import date, datetime
from pathlib import Path

import pytest

from kamra_uy.pure import opening

APP_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = APP_ROOT / "kamra_uy"
OPENING = date(2026, 12, 26)
MODULE = "kamra_uy.booking.stop_sell"


def _hooks() -> dict[str, object]:
    tree = ast.parse((PACKAGE / "hooks.py").read_text())
    return {node.targets[0].id: ast.literal_eval(node.value) for node in tree.body
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)}


def _flat(value) -> list[str]:
    return value if isinstance(value, list) else [value]


def _defined(dotted: str) -> bool:
    module, _, func = dotted.rpartition(".")
    tree = ast.parse((PACKAGE.parent / (module.replace(".", "/") + ".py")).read_text())
    return any(isinstance(n, ast.FunctionDef) and n.name == func for n in tree.body)


# --- las noches cerradas -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("hoy, desde, hasta", [
    (date(2026, 12, 20), date(2026, 12, 20), date(2026, 12, 25)),  # D-HR-39: la venta abre el 20, la apertura el 26
    (date(2026, 11, 21), date(2026, 11, 21), date(2026, 12, 25)),
    ("2026-12-24", date(2026, 12, 24), date(2026, 12, 25)),
    (datetime(2026, 12, 25, 23, 59), date(2026, 12, 25), date(2026, 12, 25)),  # la víspera: solo esa noche
])
def test_de_hoy_a_la_vispera_de_la_apertura(hoy, desde, hasta) -> None:
    assert opening.cierre(hoy, OPENING) == (desde, hasta)
    assert opening.cierre(hoy, "2026-12-26") == (desde, hasta)


@pytest.mark.parametrize("hoy", [date(2026, 12, 26), date(2026, 12, 27), "2027-03-01"])
def test_el_dia_de_la_apertura_o_despues_no_hay_cierre(hoy) -> None:
    assert opening.cierre(hoy, OPENING) is None


@pytest.mark.parametrize("apertura", [None, "", "   ", "no es una fecha", "2026-02-30"])
def test_sin_apertura_no_hay_cierre(apertura) -> None:
    assert opening.cierre(date(2026, 12, 20), apertura) is None


@pytest.mark.parametrize("hoy", [None, "", "ayer"])
def test_sin_hoy_no_se_cierra_nada(hoy) -> None:
    assert opening.cierre(hoy, OPENING) is None


def test_el_cierre_no_choca_con_el_guardian_de_la_web() -> None:
    """La primera noche que se vende es la de la apertura: la misma llegada que el guardián de HR-263 acepta."""
    _desde, hasta = opening.cierre(date(2026, 12, 20), OPENING)
    assert opening.antes_de_abrir(hasta, OPENING)
    assert not opening.antes_de_abrir(date(2026, 12, 26), OPENING)


# --- hoy: el más temprano de los dos relojes ----------------------------------------------------------------------

@pytest.mark.parametrize("sitio, propiedad, esperado", [
    (date(2026, 12, 20), date(2026, 12, 20), date(2026, 12, 20)),
    (date(2026, 12, 21), date(2026, 12, 20), date(2026, 12, 20)),  # el sitio en UTC, el hotel en Montevideo de noche
    (date(2026, 12, 20), date(2026, 12, 21), date(2026, 12, 20)),  # el hotel adelantado
    ("2026-12-20", datetime(2026, 12, 21, 1, 0), date(2026, 12, 20)),
    (date(2026, 12, 20), None, date(2026, 12, 20)),
    (None, date(2026, 12, 20), date(2026, 12, 20)),
    (None, None, None),
])
def test_hoy_es_el_mas_temprano_de_los_dos_relojes(sitio, propiedad, esperado) -> None:
    assert opening.hoy(sitio, propiedad) == esperado


# --- de quién es la fila ------------------------------------------------------------------------------------------

def test_el_nombre_es_determinista_y_lleva_el_prefijo_de_kamra_uy() -> None:
    nombre = opening.nombre_cierre("Hotel Roosevelt")
    assert nombre == opening.nombre_cierre("Hotel Roosevelt")
    assert nombre.startswith(opening.PREFIJO_CIERRE)
    assert opening.es_cierre(nombre)
    assert nombre != opening.nombre_cierre("Hotel Roosevelt 2")


def test_el_nombre_cabe_en_un_name_de_frappe_con_cualquier_propiedad() -> None:
    for propiedad in ("A", "Hotel Roosevelt", "x" * 140, "Posada <La Ñ> & Cía. / Colonia"):
        nombre = opening.nombre_cierre(propiedad)
        assert len(nombre) <= 140
        assert not set(nombre) & set("<>%/\\\n"), nombre


@pytest.mark.parametrize("nombre", [None, "", "a1b2c3d4e5", "Restriction 1", "uy-apertura", "X-UY-APERTURA-1"])
def test_una_fila_de_kamra_no_es_de_kamra_uy(nombre) -> None:
    """Las de la pantalla de restricciones nacen con el nombre al azar de Kamra (autoname hash)."""
    assert not opening.es_cierre(nombre)


@pytest.mark.parametrize("nombre", ["UY-APERTURA-1", "uy-apertura-abc"])
def test_el_prefijo_vale_en_mayusculas_o_minusculas(nombre) -> None:
    """MariaDB compara los name sin distinguir mayúsculas: la marca tampoco."""
    assert opening.es_cierre(nombre)


def test_la_nota_de_la_fila_en_espanol_neutro() -> None:
    import re

    assert opening.NOTA_CIERRE and opening.CIERRE_BLOQUEADO
    for texto in (opening.NOTA_CIERRE, opening.CIERRE_BLOQUEADO):
        assert not re.search(r"\b(podés|cambiá|vos|vaciá)\b", texto), texto
        assert "fecha de apertura" in texto


# --- el cableado (hooks.py) ---------------------------------------------------------------------------------------

def test_guardar_renombrar_o_borrar_la_propiedad_pone_al_dia_el_cierre() -> None:
    events = _hooks()["doc_events"]["Property"]
    assert f"{MODULE}.on_property_update" in _flat(events["on_update"])
    assert f"{MODULE}.on_property_rename" in _flat(events["after_rename"])
    assert f"{MODULE}.on_property_trash" in _flat(events["on_trash"])


def test_la_fila_de_kamra_uy_no_se_edita_ni_se_borra_a_mano() -> None:
    events = _hooks()["doc_events"]["Channel Restriction"]
    assert f"{MODULE}.protect" in _flat(events["validate"])
    assert f"{MODULE}.protect" in _flat(events["on_trash"])


def test_cada_hora_se_corre_el_cierre_y_se_retira_al_abrir() -> None:
    """Quién lo arranca: el programador de Frappe, a los 5 minutos de cada hora (el día cambia en la zona del sitio
    o en la del hotel; la apertura que llega retira la fila sin que nadie guarde la propiedad)."""
    assert _hooks()["scheduler_events"]["cron"]["5 * * * *"] == [f"{MODULE}.sync_all"]


def test_cada_manejador_del_cierre_existe() -> None:
    hooks = _hooks()
    handlers = [h for dt in ("Property", "Channel Restriction") for v in hooks["doc_events"][dt].values()
                for h in _flat(v) if h.startswith(MODULE)]
    handlers += hooks["scheduler_events"]["cron"]["5 * * * *"]
    assert len(handlers) == 6
    assert [h for h in handlers if not _defined(h)] == []
