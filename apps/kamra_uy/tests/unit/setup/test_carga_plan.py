"""El plan de la carga (HR-33): qué crearía, cambiaría, dejaría igual o retiraría en Kamra, sin frappe."""

import datetime

import pytest

from kamra_uy.setup import plan, planilla
from kamra_uy.setup.tests import fabrica

PROP = "Hotel Inventado"
D = datetime.date


@pytest.fixture
def p(tmp_path):
	ruta = tmp_path / "planilla.xlsx"
	fabrica.planilla_inventada(ruta)
	out = planilla.lee(ruta)
	assert out.errores == []
	return out


def _propiedad(**kw):
	base = {
		"name": PROP,
		"country": "India",
		"currency": "INR",
		"timezone": "Asia/Kolkata",
		"booking_payment_mode": "Pay at hotel",
		"deposit_pct": 0,
		"hold_minutes": 120,
		**_FABRICA_KAMRA,
	}
	base.update(kw)
	return base


# lo que la pestaña «Seña y cancelación» inventada de la fábrica pone en la propiedad (HR-290)
CONDICIONES = {
	"hold_minutes": 45,
	"checkin_time": "15:30:00",
	"checkout_time": "10:15:00",
	"free_cancel_days": 9,
	"cancellation_fee": "Full Stay",
	"no_show_charge": "First Night",
	"pets_allowed": 0,
}
# lo que Kamra trae de fábrica en una propiedad nueva (property.json)
_FABRICA_KAMRA = {
	"checkin_time": datetime.timedelta(hours=14),
	"checkout_time": datetime.timedelta(hours=11),
	"free_cancel_days": 1,
	"cancellation_fee": "First Night",
	"no_show_charge": "First Night",
	"pets_allowed": 0,
	"security_deposit_amount": 5000,
}

# lo que la carga cambia de una propiedad recién creada (lo demás ya está como lo pide la planilla)
_CAMBIAN = {k: v for k, v in CONDICIONES.items() if k == "hold_minutes" or _FABRICA_KAMRA.get(k) != v}
_CAMBIAN["security_deposit_amount"] = 0  # HR-232 (D-HR-64): el hotel no pide garantía


def _vacio(**kw):
	return plan.Estado(propiedad=_propiedad(**kw), reservas=0, tipos=[], habitaciones=[], temporadas=[])


def _ops(pl, doctype):
	return [a.op for a in pl.acciones if a.doctype == doctype]


def _cargado(pl) -> plan.Estado:
	"""El estado de Kamra si se aplicara `pl` tal cual (lo que haría carga.py), para probar la idempotencia."""
	prop = dict(pl.estado.propiedad)
	tipos = {t["name"]: dict(t) for t in pl.estado.tipos}
	habs = {h["name"]: dict(h) for h in pl.estado.habitaciones}
	tems = {t["name"]: dict(t) for t in pl.estado.temporadas}
	nombres = {}
	for a in pl.acciones:
		if a.doctype == "Property":
			prop.update(a.valores)
		elif a.doctype == "Room Type":
			nombre = a.nombre or f"{PROP}-{a.valores['room_type_code']}"
			nombres[a.tipo] = nombre
			tipos.setdefault(nombre, {"name": nombre}).update(a.valores)
	resolver = {**{t["room_type_name"]: t["name"] for t in tipos.values()}, **nombres}
	for a in pl.acciones:
		valores = dict(a.valores)
		if "room_type" in valores and valores["room_type"] in resolver:
			valores["room_type"] = resolver[valores["room_type"]]
		if a.doctype == "Room":
			nombre = a.nombre or f"{PROP}-{valores['room_number']}"
			habs.setdefault(nombre, {"name": nombre}).update(valores)
		elif a.doctype == "Season":
			nombre = a.nombre or f"SEA-{len(tems) + 1:05d}"
			tems.setdefault(nombre, {"name": nombre, "priority": 0}).update(valores)
	return plan.Estado(prop, pl.estado.reservas, list(tipos.values()), list(habs.values()), list(tems.values()))


def test_on_an_empty_property_it_creates_everything_and_sets_full_payment(p) -> None:
	pl = plan.planea(p, _vacio())
	assert pl.errores == []
	(prop,) = [a for a in pl.acciones if a.doctype == "Property"]
	assert prop.op == "cambiar"
	assert prop.valores == {
		"country": "Uruguay",
		"booking_payment_mode": "Full online",
		"deposit_pct": 100,
		**_CAMBIAN,
		"timezone": "America/Montevideo",
		"currency": "USD",
	}
	assert _ops(pl, "Room Type") == ["crear"] * 3
	assert _ops(pl, "Room") == ["crear"] * 6
	assert _ops(pl, "Season") == ["crear"] * 9
	assert pl.recuento() == {
		"Property": {"cambiar": 1},
		"Room Type": {"crear": 3},
		"Room": {"crear": 6},
		"Season": {"crear": 9},
	}


def test_room_types_carry_capacity_and_the_lowest_price_as_base(p) -> None:
	pl = plan.planea(p, _vacio())
	tipos = {a.tipo: a.valores for a in pl.acciones if a.doctype == "Room Type"}
	assert tipos["Triple"] == {
		"room_type_name": "Triple",
		"room_type_code": "TRIPLE",
		"base_price": 702,
		"adults_capacity": 3,
		"base_occupancy": 3,
		"children_capacity": 2,
		"max_total_occupants": 3,
		"children_within_base_occupancy": 1,
		"child_price": 0,
		"extra_adult_price": 0,
	}
	assert tipos["Apartamento"]["room_type_code"] == "APARTAMENTO"


def test_people_not_adults_children_up_to_capacity_minus_one_and_the_total_capped(p) -> None:
	# HR-290, el fundador (22-11): «la capacidad maxima de la habitacion -1 es la cantidad de niños»; el total, la capacidad
	pl = plan.planea(p, _vacio())
	tipos = {a.tipo: a.valores for a in pl.acciones if a.doctype == "Room Type"}
	assert {t: (v["adults_capacity"], v["children_capacity"], v["max_total_occupants"]) for t, v in tipos.items()} == {
		"Doble": (2, 1, 2),
		"Triple": (3, 2, 3),
		"Apartamento": (5, 4, 5),
	}


def test_a_written_children_maximum_wins_over_the_rule(tmp_path) -> None:
	hab = [list(f) for f in fabrica.HABITACIONES]
	for f in hab:
		if f[1] == "Triple":
			f[4] = 1
	ruta = tmp_path / "p.xlsx"
	fabrica.escribe_xlsx(ruta, fabrica.pestanas(habitaciones=hab))
	pl = plan.planea(planilla.lee(ruta), _vacio())
	(triple,) = [a.valores for a in pl.acciones if a.doctype == "Room Type" and a.tipo == "Triple"]
	assert (triple["children_capacity"], triple["max_total_occupants"]) == (1, 3)


def test_children_priced_per_person_leave_the_child_prices_alone(tmp_path) -> None:
	sena = [list(f) for f in fabrica.sena()]
	sena[8][1] = "Menores de 5 gratis"
	ruta = tmp_path / "p.xlsx"
	fabrica.escribe_xlsx(ruta, fabrica.pestanas(sena_=sena))
	pl = plan.planea(planilla.lee(ruta), _vacio())
	(triple,) = [a.valores for a in pl.acciones if a.doctype == "Room Type" and a.tipo == "Triple"]
	assert not {"child_price", "extra_adult_price", "children_within_base_occupancy"} & set(triple)


def test_rooms_carry_type_floor_and_beds_with_notes(p) -> None:
	pl = plan.planea(p, _vacio())
	habs = {a.valores["room_number"]: a.valores for a in pl.acciones if a.doctype == "Room"}
	assert habs["801"] == {
		"room_number": "801",
		"room_type": "Doble",
		"floor": "8",
		"notes": "2 camas individuales · Vista inventada A",
	}
	assert habs["802"]["notes"] == "2 camas individuales"


def test_seasons_are_absolute_per_type_with_no_advance_percent(p) -> None:
	pl = plan.planea(p, _vacio())
	(alta_doble,) = [
		a for a in pl.acciones
		if a.doctype == "Season" and a.valores["season_name"] == "Alta inventada" and a.valores["room_type"] == "Doble"
	]
	assert alta_doble.valores == {
		"season_name": "Alta inventada",
		"room_type": "Doble",
		"start_date": D(2030, 1, 1),
		"end_date": D(2030, 1, 10),
		"adjustment_type": "Absolute",
		"adjustment_value": 1001,
		"minimum_nights": 3,
		"advance_percent": 0,
		"full_payment": 0,
		"disabled": 0,
	}
	assert all(a.valores["advance_percent"] == 0 for a in pl.acciones if a.doctype == "Season")


def test_a_second_run_changes_nothing(p) -> None:
	primera = plan.planea(p, _vacio())
	segunda = plan.planea(p, _cargado(primera))
	assert segunda.errores == []
	assert {op for a in segunda.acciones for op in [a.op]} == {"igual"}
	assert segunda.recuento() == {
		"Property": {"igual": 1},
		"Room Type": {"igual": 3},
		"Room": {"igual": 6},
		"Season": {"igual": 9},
	}


def test_a_changed_price_updates_that_season_and_creates_nothing(p, tmp_path) -> None:
	cargado = _cargado(plan.planea(p, _vacio()))
	ruta = tmp_path / "otra.xlsx"
	fabrica.planilla_inventada(ruta, precio_doble=1111)
	pl = plan.planea(planilla.lee(ruta), cargado)
	cambios = [a for a in pl.acciones if a.op != "igual"]
	assert [(a.doctype, a.op, a.valores["adjustment_value"], a.antes["adjustment_value"]) for a in cambios] == [
		("Season", "cambiar", 1111, 1001)
	]
	assert cambios[0].nombre  # actualiza el registro que ya existe


def test_moved_dates_create_the_new_season_and_retire_the_old_one(p) -> None:
	cargado = _cargado(plan.planea(p, _vacio()))
	for t in cargado.temporadas:
		if t["season_name"] == "Baja inventada":
			t["end_date"] = D(2030, 3, 15)
	pl = plan.planea(p, cargado)
	cambios = sorted((a.op, a.valores.get("room_type", ""), a.doctype) for a in pl.acciones if a.op != "igual")
	assert [c[0] for c in cambios] == ["crear"] * 3 + ["retirar"] * 3
	assert all(a.valores == {"disabled": 1} for a in pl.acciones if a.op == "retirar")


def test_a_season_dropped_to_no_dates_is_retired(p) -> None:
	cargado = _cargado(plan.planea(p, _vacio()))
	cargado.temporadas.append({
		"name": "SEA-09999", "season_name": "Julio inventado", "room_type": f"{PROP}-DOBLE",
		"start_date": D(2030, 7, 1), "end_date": D(2030, 7, 15), "adjustment_type": "Absolute",
		"adjustment_value": 601, "minimum_nights": 2, "advance_percent": 0, "full_payment": 0, "disabled": 0,
		"priority": 0,
	})
	pl = plan.planea(p, cargado)
	assert [(a.op, a.nombre) for a in pl.acciones if a.op != "igual"] == [("retirar", "SEA-09999")]


def test_a_season_with_advance_percent_is_cleared(p) -> None:
	cargado = _cargado(plan.planea(p, _vacio()))
	cargado.temporadas[0]["advance_percent"] = 50
	pl = plan.planea(p, cargado)
	(cambio,) = [a for a in pl.acciones if a.op != "igual"]
	assert (cambio.op, cambio.valores["advance_percent"], cambio.antes["advance_percent"]) == ("cambiar", 0, 50)


def test_a_foreign_season_with_advance_percent_is_a_warning_and_is_not_touched(p) -> None:
	cargado = _cargado(plan.planea(p, _vacio()))
	cargado.temporadas.append({
		"name": "SEA-07777", "season_name": "Puesta a mano", "room_type": None, "start_date": D(2030, 1, 5),
		"end_date": D(2030, 1, 6), "adjustment_type": "Percent", "adjustment_value": 0, "minimum_nights": 0,
		"advance_percent": 30, "full_payment": 0, "disabled": 0, "priority": 0,
	})
	pl = plan.planea(p, cargado)
	assert {a.op for a in pl.acciones} == {"igual"}
	assert any("«Puesta a mano» (SEA-07777) pide un anticipo del 30 %" in t for t in pl.avisos)


def test_existing_types_are_matched_by_name_ignoring_case(p) -> None:
	estado = _vacio()
	estado.tipos.append({
		"name": f"{PROP}-DBL", "room_type_name": "doble", "room_type_code": "DBL", "base_price": 9,
		"adults_capacity": 2, "base_occupancy": 2, "children_capacity": 1,
	})
	pl = plan.planea(p, estado)
	(doble,) = [a for a in pl.acciones if a.doctype == "Room Type" and a.tipo == "Doble"]
	assert (doble.op, doble.nombre) == ("cambiar", f"{PROP}-DBL")
	assert doble.valores == {
		"room_type_name": "Doble", "base_price": 501, "max_total_occupants": 2, "children_within_base_occupancy": 1
	}
	habs = [a for a in pl.acciones if a.doctype == "Room" and a.valores["room_number"] == "801"]
	assert habs[0].valores["room_type"] == "Doble"


def test_kamra_records_missing_from_the_planilla_are_warned_and_kept(p) -> None:
	estado = _vacio()
	estado.tipos.append({"name": f"{PROP}-STE", "room_type_name": "Suite", "room_type_code": "STE", "base_price": 1,
	                     "adults_capacity": 2, "base_occupancy": 2, "children_capacity": 1})
	estado.habitaciones.append({"name": f"{PROP}-999", "room_number": "999", "room_type": f"{PROP}-STE",
	                            "floor": None, "notes": None})
	pl = plan.planea(p, estado)
	assert "En Kamra hay un tipo que la planilla no trae: Suite (no se toca)." in pl.avisos
	assert "En Kamra hay 1 habitación que la planilla no trae: 999 (no se toca)." in pl.avisos


def test_a_code_clash_gets_a_suffix(p) -> None:
	estado = _vacio()
	estado.tipos.append({"name": f"{PROP}-DOBLE", "room_type_name": "Otra cosa", "room_type_code": "DOBLE",
	                     "base_price": 1, "adults_capacity": 2, "base_occupancy": 2, "children_capacity": 1})
	pl = plan.planea(p, estado)
	(doble,) = [a for a in pl.acciones if a.doctype == "Room Type" and a.tipo == "Doble"]
	assert (doble.op, doble.valores["room_type_code"]) == ("crear", "DOBLE2")


def test_the_property_must_exist(p) -> None:
	pl = plan.planea(p, plan.Estado(None, 0, [], [], []), propiedad=PROP)
	assert pl.errores == [
		f"La propiedad «{PROP}» no existe en Kamra: créala con el asistente de Kamra y vuelve a correr la carga."
	]
	assert pl.acciones == []


def test_a_different_currency_with_bookings_is_an_error(p) -> None:
	estado = _vacio()
	estado.reservas = 3
	pl = plan.planea(p, estado)
	assert pl.errores == [
		f"«{PROP}» cobra en INR y la planilla trae precios en USD; ya tiene 3 reservas, así que la carga no cambia la "
		"moneda: decídelo en Kamra."
	]


def test_a_set_timezone_is_kept_and_the_conditions_come_from_the_planilla(p) -> None:
	pl = plan.planea(p, _vacio(timezone="America/Montevideo", hold_minutes=45, currency="USD", country="Uruguay"))
	(prop,) = [a for a in pl.acciones if a.doctype == "Property"]
	assert prop.valores == {
		"booking_payment_mode": "Full online", "deposit_pct": 100, **{k: v for k, v in _CAMBIAN.items() if k != "hold_minutes"}
	}


def test_kamra_times_as_timedelta_or_text_compare_with_the_planilla(p) -> None:
	ya = {**CONDICIONES, "checkin_time": datetime.timedelta(hours=15, minutes=30), "checkout_time": "10:15:00"}
	pl = plan.planea(p, _vacio(timezone="America/Montevideo", currency="USD", country="Uruguay",
	                           booking_payment_mode="Full online", deposit_pct=100, security_deposit_amount=0, **ya))
	(prop,) = [a for a in pl.acciones if a.doctype == "Property"]
	assert (prop.op, prop.valores) == ("igual", {})


def _sin(tmp_path, *indices):
	sena = [list(f) for f in fabrica.sena()]
	for i in indices:
		sena[i][1] = None
	ruta = tmp_path / "p.xlsx"
	fabrica.escribe_xlsx(ruta, fabrica.pestanas(sena_=sena))
	return planilla.lee(ruta)


def test_a_blank_condition_is_left_as_kamra_has_it(tmp_path) -> None:
	pl = plan.planea(_sin(tmp_path, 3, 5, 9), _vacio())
	(prop,) = [a for a in pl.acciones if a.doctype == "Property"]
	assert not {"checkin_time", "free_cancel_days", "pets_allowed"} & set(prop.valores)


def test_a_blank_hold_on_an_empty_kamra_hold_gets_the_default(tmp_path) -> None:
	pl = plan.planea(_sin(tmp_path, 2), _vacio(hold_minutes=0))
	(prop,) = [a for a in pl.acciones if a.doctype == "Property"]
	assert prop.valores["hold_minutes"] == 120


def test_cancellation_tiers_in_kamra_override_the_window_and_are_a_warning(p) -> None:
	estado = _vacio()
	estado.tramos_cancelacion = 2
	pl = plan.planea(p, estado)
	assert any("tiene 2 tramos de cancelación" in t and "mandan sobre los 9 días" in t for t in pl.avisos)


def test_planilla_errors_stop_the_plan(tmp_path) -> None:
	tem = fabrica.temporadas()
	tem[1][4] = "caro"
	ruta = tmp_path / "mala.xlsx"
	fabrica.escribe_xlsx(ruta, fabrica.pestanas(temporadas_=tem))
	pl = plan.planea(planilla.lee(ruta), _vacio())
	assert pl.acciones == []
	assert pl.errores == ["Temporadas y precios, fila 5, columna E (Precio por noche (US$)): «caro» no es un precio."]


def test_the_report_is_spanish_and_says_what_it_would_do(p) -> None:
	texto = plan.planea(p, _vacio()).texto(aplicado=False)
	assert texto.splitlines()[0] == f"Carga de la planilla en «{PROP}» — PRUEBA: no se ha escrito nada."
	assert "Planilla: 6 habitaciones (Apartamento 1, Doble 2, Triple 3) · 3 temporadas con fechas (9 precios) · USD" in texto
	assert "  booking_payment_mode: Pay at hotel → Full online" in texto
	assert "  free_cancel_days: 1 → 9" in texto
	assert "  checkin_time: 14:00:00 → 15:30:00" in texto
	assert "  + Doble (DOBLE): precio base 501 USD, 2 personas (niños hasta 1)" in texto
	assert "  + 801: Doble, piso 8" in texto
	assert "  + Alta inventada, 01-01-2030 → 10-01-2030, Doble: 1001 USD/noche, mínimo 3 noches" in texto
	assert "«Julio inventado» no tiene fechas" in texto
	assert texto.splitlines()[-1] == "RESULTADO: lista para cargar (crearía 18, cambiaría 1, igual 0, retiraría 0)."


def test_the_report_after_applying(p) -> None:
	texto = plan.planea(p, _vacio()).texto(aplicado=True)
	assert texto.splitlines()[0] == f"Carga de la planilla en «{PROP}» — APLICADA."
	assert texto.splitlines()[-1] == "RESULTADO: cargada (creados 18, cambiados 1, igual 0, retirados 0)."


def test_the_report_with_errors(tmp_path) -> None:
	tem = fabrica.temporadas()
	tem[1][4] = "caro"
	ruta = tmp_path / "mala.xlsx"
	fabrica.escribe_xlsx(ruta, fabrica.pestanas(temporadas_=tem))
	texto = plan.planea(planilla.lee(ruta), _vacio()).texto(aplicado=False)
	assert texto.splitlines()[-1] == "RESULTADO: NO SE PUEDE CARGAR: 1 error en la planilla o en Kamra (arriba)."


# D-HR-81 (HR-367): la Triple con precio para 2 y para 3 en cada temporada
_TRIPLES = {1: 1302, 4: 1002, 7: 602}  # las filas de la Triple con fechas en fabrica.temporadas()


def _con_precio2(tmp_path, precios2):
	cabecera, tem = fabrica.con_precio2(fabrica.temporadas(), precios2)
	ruta = tmp_path / "precio2.xlsx"
	fabrica.escribe_xlsx(ruta, fabrica.pestanas(temporadas_=tem, tem_cabecera=cabecera))
	out = planilla.lee(ruta)
	assert out.errores == []
	return out


def _resumen(pl):
	return [(a.doctype, a.op, a.nombre, a.tipo, a.valores, a.antes) for a in pl.acciones]


def test_without_a_price_for_two_the_plan_is_todays(p, tmp_path) -> None:
	vacia = _con_precio2(tmp_path, {})
	assert _resumen(plan.planea(vacia, _vacio())) == _resumen(plan.planea(p, _vacio()))
	# un Kamra ya cargado, con los campos nuevos como los deja migrate (0) o sin leer (None): todo igual
	cargado = _cargado(plan.planea(p, _vacio()))
	for i, t in enumerate(cargado.tipos):
		t["single_occupancy_price"] = (0, None)[i % 2]
	for i, s in enumerate(cargado.temporadas):
		s.update(priced_by_occupancy=0, extra_adult_price=(0, None)[i % 2], child_price=0)
	for planilla_ in (p, vacia):
		assert {a.op for a in plan.planea(planilla_, cargado).acciones} == {"igual"}


def test_the_triple_is_priced_for_two_and_three_and_nothing_else_moves(p, tmp_path) -> None:
	hoy = plan.planea(p, _vacio())
	pl = plan.planea(_con_precio2(tmp_path, _TRIPLES), _vacio())
	assert pl.errores == []
	tipos = {a.tipo: a.valores for a in pl.acciones if a.doctype == "Room Type"}
	assert tipos["Triple"] == {
		"room_type_name": "Triple",
		"room_type_code": "TRIPLE",
		"base_price": 602,
		"adults_capacity": 3,
		"base_occupancy": 2,
		"children_capacity": 2,
		"max_total_occupants": 3,
		"children_within_base_occupancy": 1,
		"child_price": 100,
		"extra_adult_price": 100,
		"single_occupancy_price": 0,
	}
	triples = [a.valores for a in pl.acciones if a.doctype == "Season" and a.tipo == "Triple"]
	assert [(v["season_name"], v["adjustment_type"], v["adjustment_value"], v["priced_by_occupancy"],
	         v["extra_adult_price"], v["child_price"]) for v in triples] == [
		("Alta inventada", "Absolute", 1302, 1, 200, 200),
		("Media inventada", "Absolute", 1002, 1, 200, 200),
		("Baja inventada", "Absolute", 602, 1, 100, 100),
	]
	otras = [x for x in _resumen(pl) if x[3] != "Triple"]
	assert otras == [x for x in _resumen(hoy) if x[3] != "Triple"]


def test_on_a_loaded_kamra_only_the_triple_and_its_seasons_change(p, tmp_path) -> None:
	cargado = _cargado(plan.planea(p, _vacio()))
	pl = plan.planea(_con_precio2(tmp_path, _TRIPLES), cargado)
	cambios = [(a.doctype, a.tipo, a.valores, a.antes) for a in pl.acciones if a.op != "igual"]
	assert cambios[0] == ("Room Type", "Triple",
	                      {"base_price": 602, "base_occupancy": 2, "child_price": 100, "extra_adult_price": 100},
	                      {"base_price": 702, "base_occupancy": 3, "child_price": 0, "extra_adult_price": 0})
	assert [(d, t, v["adjustment_value"], v["priced_by_occupancy"], v["extra_adult_price"]) for d, t, v, _ in
	        cambios[1:]] == [("Season", "Triple", 1302, 1, 200), ("Season", "Triple", 1002, 1, 200),
	                         ("Season", "Triple", 602, 1, 100)]
	assert {a.op for a in plan.planea(_con_precio2(tmp_path, _TRIPLES), _cargado(pl)).acciones} == {"igual"}
	# y quitar la columna devuelve la Triple al precio por habitación
	vuelta = plan.planea(p, _cargado(pl))
	assert [(a.doctype, a.op) for a in vuelta.acciones if a.op != "igual"] == [
		("Room Type", "cambiar"), ("Season", "cambiar"), ("Season", "cambiar"), ("Season", "cambiar")]
	assert {a.valores["priced_by_occupancy"] for a in vuelta.acciones if a.doctype == "Season" and a.op != "igual"} == {0}


def test_the_report_shows_the_price_for_two_and_for_three(p, tmp_path) -> None:
	texto = plan.planea(_con_precio2(tmp_path, _TRIPLES), _vacio()).texto(aplicado=False)
	assert "  + Triple (TRIPLE): precio base para 2 602 · para 3 702 USD, 3 personas (niños hasta 2)" in texto
	assert ("  + Alta inventada, 01-01-2030 → 10-01-2030, Triple: precio para 2 1302 · para 3 1502 USD/noche, "
	        "mínimo 3 noches") in texto
	assert "  + Alta inventada, 01-01-2030 → 10-01-2030, Doble: 1001 USD/noche, mínimo 3 noches" in texto
	cargado = _cargado(plan.planea(p, _vacio()))
	for s in cargado.temporadas:  # como los deja migrate
		s.update(priced_by_occupancy=0, extra_adult_price=0, child_price=0)
	cambio = plan.planea(_con_precio2(tmp_path, _TRIPLES), cargado).texto(aplicado=False)
	assert ("  ~ Alta inventada, 01-01-2030 → 10-01-2030, Triple: adjustment_value 1502 → 1302, priced_by_occupancy 0 → "
	        "1, extra_adult_price 0 → 200, child_price 0 → 200 (precio para 2 1302 · para 3 1502 USD)") in cambio
