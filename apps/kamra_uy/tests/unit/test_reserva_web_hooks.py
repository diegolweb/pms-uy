"""El cableado de la reserva en línea en hooks.py (sin frappe): la residencia y el IVA al reservar (HR-266, D-HR-47), y
que de la fecha de apertura y el cierre de ventas antes de abrir (HR-263 y HR-264) no quede nada (HR-275, D-HR-49: «YO
decido las fechas»; «Quítalo del todo, no lo quiero en el código»).

Lo único que puede nombrar la apertura es el patch que la borra de la base (kamra_uy/patches/v1_0/quita_apertura.py).
"""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "kamra_uy"
REPO = PACKAGE.parents[2]
PATCH = "kamra_uy.patches.v1_0.quita_apertura"


def _hooks() -> dict[str, object]:
    tree = ast.parse((PACKAGE / "hooks.py").read_text())
    return {node.targets[0].id: ast.literal_eval(node.value) for node in tree.body
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)}


def _flat(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _flat(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _flat(v)]
    return []


# --- la residencia y el IVA al reservar (HR-266) -------------------------------------------------------------------

def test_book_y_su_estado_pasan_por_la_residencia() -> None:
    # HR-275: la vitrina vuelve a ser la de Kamra tal cual; book pide la residencia en Uruguay y el estado de la reserva
    # lleva el desglose del IVA que se cobró al reservar; HR-337: la búsqueda es la de Kamra con su presupuesto por coste
    assert _hooks()["override_whitelisted_methods"] == {
        "kamra.public_api.book": "kamra_uy.booking.residence.book",
        "kamra.public_api.booking_status": "kamra_uy.booking.residence.booking_status",
        "kamra.public_api.search_stay": "kamra_uy.booking.stay_search.search_stay",
        "kamra.public_api.hosting_enquiry": "kamra_uy.closed.hosting_enquiry",
    }


def test_el_formulario_de_alojamiento_de_kamra_queda_cerrado_por_todas_las_vias() -> None:
    # HR-374: el borde niega su ruta, pero el `cmd` de Frappe la alcanza por cualquier ruta; su sustituta existe y NO
    # está en la lista blanca, así que Frappe contesta 403 sin ejecutarla.
    target = _hooks()["override_whitelisted_methods"]["kamra.public_api.hosting_enquiry"]
    module, name = target.rsplit(".", 1)
    tree = ast.parse((PACKAGE.parent / Path(*module.split("."))).with_suffix(".py").read_text())
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert name in funcs, target
    assert not [d for d in funcs[name].decorator_list if "whitelist" in ast.unparse(d)], f"{target} no puede ser pública"


def test_la_reserva_web_guarda_su_idioma_y_lo_declarado_al_crearse() -> None:
    assert _hooks()["doc_events"]["Reservation"]["before_insert"] == [
        "kamra_uy.notifications.confirmation.remember_language",
        "kamra_uy.booking.residence.remember_declaration",
    ]


def test_los_campos_se_crean_al_instalar_y_en_cada_migrate() -> None:
    hooks = _hooks()
    for hook in ("after_install", "after_migrate"):
        assert hooks[hook] == ["kamra_uy.notifications.confirmation.ensure_fields",
                               "kamra_uy.booking.residence.ensure_fields",
                               "kamra_uy.contact.form.ensure_fields",  # HR-314: el destinatario de la web
                               "kamra_uy.payments.payer.ensure_fields",  # HR-360: quien pagó
                               "kamra_uy.fiscal.agency.ensure_fields",  # HR-236: la agencia del MINTUR
                               "kamra_uy.notifications.cancelada.ensure_fields"], hook  # HR-374: el aviso de cancelación


def test_las_consultas_de_la_web_salen_de_la_cola_cada_hora() -> None:
    # HR-314: kamra_uy/contact/form.py borra las consultas ya enviadas (y las no enviadas a los 7 días)
    assert _hooks()["scheduler_events"]["hourly"] == ["kamra_uy.contact.form.purge"]


def test_el_iva_de_lo_declarado_va_antes_que_el_del_check_in() -> None:
    # la reserva con residencia declarada lleva el IVA de cada noche desde que se crea; en el check-in manda core
    # (fiscal.events) y, después de guardar, residence apunta en la cuenta la diferencia si el documento no coincide
    # HR-304: antes de todo, en Uruguay el check-in no se hace sin la residencia (require_at_check_in)
    events = _hooks()["doc_events"]["Reservation"]
    assert events["validate"] == ["kamra_uy.booking.residence.require_at_check_in",
                                  "kamra_uy.booking.residence.on_reservation_validate",
                                  "kamra_uy.fiscal.events.on_reservation_validate"]
    assert events["on_update"][-1] == "kamra_uy.booking.residence.on_reservation_update"


# --- sin apertura ni cierre de ventas (HR-275) ---------------------------------------------------------------------

def test_ningun_hook_nombra_la_apertura_ni_el_cierre_de_ventas() -> None:
    hooks = _hooks()
    nombres = [s for v in hooks.values() for s in _flat(v)]
    assert [s for s in nombres if re.search(r"opening|stop_sell", s)] == []
    assert "Property" not in hooks["doc_events"]
    assert "Channel Restriction" not in hooks["doc_events"]


def test_los_modulos_de_la_apertura_ya_no_existen() -> None:
    for modulo in ("booking/opening.py", "booking/stop_sell.py", "pure/opening.py",
                   "booking/tests/test_opening.py", "booking/tests/test_stop_sell.py"):
        assert not (PACKAGE / modulo).exists(), modulo


# lo que dejaron HR-263 y HR-264: el campo, el error del guardián, la fila del cierre, la regla de la web y su texto
RASTRO = re.compile(r"uy_opening_date|BeforeOpening|UY-APERTURA|aperturaFutura|fechas\.apertura|booking\.opening"
                    r"|booking\.stop_sell|pure\.opening|antes_de_abrir")
DONDE = ["apps/kamra_uy/kamra_uy", "apps/kamra_uy/tests", "web/src", "web/content", "web/tests", "web/scripts",
         "tests/e2e", "deploy/dev"]
SALVO = {Path(__file__).resolve(), PACKAGE / "patches" / "v1_0" / "quita_apertura.py",
         PACKAGE / "patches" / "v1_0" / "tests" / "test_quita_apertura.py"}


def test_no_queda_rastro_de_la_apertura_en_el_codigo() -> None:
    restos = []
    for base in DONDE:
        for f in (REPO / base).rglob("*"):
            if not f.is_file() or f.resolve() in SALVO or "__pycache__" in f.parts or "node_modules" in f.parts:
                continue
            try:
                texto = f.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            restos += [f"{f.relative_to(REPO)}:{n}" for n, linea in enumerate(texto.splitlines(), 1)
                       if RASTRO.search(linea)]
    assert restos == []


def test_los_e2e_de_la_apertura_ya_no_existen() -> None:
    for check in ("hr263-apertura.sh", "hr264-plataformas-apertura.sh"):
        assert not (REPO / "tests" / "e2e" / "checks" / check).exists(), check


# --- el patch que la borra de la base --------------------------------------------------------------------------------

def test_el_patch_que_la_borra_de_la_base_va_tras_migrar_los_doctypes() -> None:
    lineas = [ln.strip() for ln in (PACKAGE / "patches.txt").read_text().splitlines()]
    assert PATCH in lineas
    assert lineas.index(PATCH) > lineas.index("[post_model_sync]")
    fuente = (PACKAGE / "patches" / "v1_0" / "quita_apertura.py").read_text()
    assert "def execute(" in fuente
    assert (PACKAGE / "patches" / "v1_0" / "__init__.py").exists()


def test_el_patch_nombra_lo_que_borra() -> None:
    # sin frappe no se importa: se lee
    tree = ast.parse((PACKAGE / "patches" / "v1_0" / "quita_apertura.py").read_text())
    valores = {node.targets[0].id: ast.literal_eval(node.value) for node in tree.body
               if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
               and isinstance(node.value, ast.Constant)}
    assert valores["CUSTOM_FIELD"] == "Property-uy_opening_date"
    assert valores["PREFIX"] == "UY-APERTURA-"


def test_ningun_modulo_de_kamra_uy_importa_lo_quitado() -> None:
    # los nombres de módulo que importa cada .py del paquete (sin importarlos: piden frappe)
    malos = []
    for f in PACKAGE.rglob("*.py"):
        if f.resolve() in SALVO:
            continue
        for node in ast.walk(ast.parse(f.read_text())):
            nombres = ([a.name for a in node.names] if isinstance(node, ast.Import)
                       else [f"{node.module}.{a.name}" for a in node.names] if isinstance(node, ast.ImportFrom) and node.module
                       else [])
            malos += [f"{f.relative_to(PACKAGE)}: {n}" for n in nombres if re.search(r"\b(opening|stop_sell)\b", n)]
    assert malos == []
    assert importlib.util.find_spec("kamra_uy.pure.opening") is None
