"""Las reservas ya tomadas en la carga (HR-33), sin frappe: la pestaña se abre solo si se pide; cada reserva se crea una
vez, con el `ota_ref` que luego usa Channex (o su clave de la carga si es directa); una segunda corrida no crea nada ni
pisa lo que Kamra cambió; lo que no cabe es un error antes de escribir; y el informe no copia datos del huésped."""

import datetime

import pytest

from kamra_uy.setup import plan, planilla
from kamra_uy.setup.tests import fabrica

PROP = "Hotel Inventado"
D = datetime.date
HOY = D(2029, 12, 1)


def _lee(tmp_path, filas=None, reservas=True):
	ruta = tmp_path / "planilla.xlsx"
	fabrica.escribe_xlsx(ruta, fabrica.pestanas(reservas_=fabrica.reservas() if filas is None else filas))
	return planilla.lee(ruta, reservas=reservas)


def _textos(notas):
	return [n.texto() for n in notas]


def _vacio(**kw):
	prop = {"name": PROP, "currency": "USD", "timezone": "America/Montevideo", "security_deposit_amount": 0}
	return plan.Estado(propiedad=prop, reservas=0, tipos=[], habitaciones=[], temporadas=[], **kw)


def _reservas(pl, op=None):
	return [a for a in pl.acciones if a.doctype == "Reservation" and (op is None or a.op == op)]


def _fila(**cambios):
	"""Una fila bajo RES_CABECERA: la primera reserva inventada (Booking.com, Doble del 3 al 6-01-2030) con cambios."""
	fila = list(fabrica.reservas()[0])
	for k, v in cambios.items():
		fila[fabrica.RES_CABECERA.index(k)] = v
	return fila


def _cargado(pl) -> list[dict]:
	"""Las reservas que dejaría en Kamra aplicar `pl` (lo que hace carga.py), como las lee `carga.estado`."""
	tipos = {t: f"{PROP}-{t.upper()}" for t in pl.planilla.tipos}
	return [
		{
			"name": f"RES-2030-{n:05d}",
			"status": "Confirmed",
			"ota_ref": a.valores["ota_ref"],
			"idempotency_key": a.valores["idempotency_key"],
			"room_type": tipos[a.valores["room_type"]],
			"room": None,
			"check_in_date": a.valores["check_in_date"],
			"check_out_date": a.valores["check_out_date"],
			"adults": a.valores["adults"],
			"children": a.valores["children"],
		}
		for n, a in enumerate(_reservas(pl, "crear"), start=1)
	]


def _con_tipos(pl, **kw) -> plan.Estado:
	"""Kamra con los tipos y habitaciones de la planilla ya cargados (nombres como los de `_cargado`)."""
	estado = _vacio(**kw)
	estado.tipos = [{"name": f"{PROP}-{t.upper()}", "room_type_name": t, "room_type_code": t.upper()}
	                for t in pl.planilla.tipos]
	estado.habitaciones = [{"name": f"{PROP}-{h.numero}", "room_number": h.numero, "room_type": f"{PROP}-{h.tipo.upper()}"}
	                       for h in pl.planilla.habitaciones]
	return estado


# --- la pestaña

def test_the_reservations_tab_is_opened_only_when_asked(tmp_path) -> None:
	ruta = tmp_path / "planilla.xlsx"
	fabrica.planilla_inventada(ruta)  # la de reservas, ROTA: abrirla tumbaría la lectura
	p = planilla.lee(ruta)
	assert (p.errores, p.con_reservas, p.reservas) == ([], False, [])
	assert "reservas" not in p.recuento()
	p = planilla.lee(ruta, reservas=True)
	assert any("dañada" in t for t in _textos(p.errores))


def test_the_invented_reservations_read_with_their_keys(tmp_path) -> None:
	p = _lee(tmp_path)
	assert p.errores == []
	leidas = [(r.ref, r.plataforma, r.ota, r.origen, r.tipo, r.habitacion, r.adultos, r.ninos, r.total) for r in p.reservas]
	assert leidas == [
		("4100000001", "Booking.com", True, "OTA", "Doble", None, 2, 0, 2400),
		("4100000002", "Booking.com", True, "OTA", "Triple", None, 2, 1, None),  # «4100.000.002», «Booking»
		("EXP-INV-77-0", "Expedia", True, "OTA", "Triple", None, 3, 0, 1500),  # dos habitaciones: como Channex
		("EXP-INV-77-1", "Expedia", True, "OTA", "Doble", None, 2, 0, 1200),
		("HMINVENT04", "Airbnb", True, "OTA", "Apartamento", "806", 4, 1, None),
		("T-0005", "Directa", False, "Phone", "Doble", "802", 2, 0, 1000),
	]
	assert p.canceladas == [("4100000006", "Booking.com", True)]
	r = p.recuento()
	assert (r["reservas"], r["reservas_por_plataforma"], r["reservas_canceladas"]) == (
		6, {"Airbnb": 1, "Booking.com": 2, "Directa": 1, "Expedia": 2}, 1)
	assert any("EXP-INV-77 (Expedia) sale en 2 filas" in t for t in _textos(p.avisos))


@pytest.mark.parametrize(
	("cambios", "esperado"),
	[
		({"Código": None}, "falta el código de la reserva"),
		({"Plataforma": "Trivago"}, "«Trivago» no es una plataforma que la carga conozca"),
		({"Llegada": "a confirmar"}, "«a confirmar» no es una fecha"),
		({"Salida": D(2030, 1, 3)}, "la salida tiene que ser después de la llegada"),
		({"Tipo de habitación": "Suite"}, "«Suite» no es un tipo de la pestaña Habitaciones"),
		({"Habitación": "803"}, "la 803 es Triple, no Doble"),
		({"Habitación": "999"}, "la habitación 999 no está en la pestaña Habitaciones"),
		({"Adultos": 3}, "3 adultos y 0 niños no caben en una Doble"),
		({"Niños": 2, "Adultos": 1}, "1 adulto y 2 niños no caben en una Doble (caben 2 personas, niños hasta 1)"),
		({"Total (US$)": "gratis"}, "«gratis» no es un importe"),
		({"Estado": "No-show"}, "la carga entiende «Confirmada» o «Cancelada»"),
	],
)
def test_a_bad_row_is_an_error_with_its_row_and_column(tmp_path, cambios, esperado) -> None:
	p = _lee(tmp_path, [_fila(**cambios)])
	assert len(p.errores) == 1, _textos(p.errores)
	assert p.errores[0].pestana == "Reservas desde 15-nov" and p.errores[0].fila == 4
	assert esperado in p.errores[0].texto()


def test_guest_data_never_goes_into_a_message(tmp_path) -> None:
	p = _lee(tmp_path, [_fila(Correo="uno-en-example.test")])
	(error,) = _textos(p.errores)
	assert "el correo no es válido" in error
	assert "uno-en-example" not in error


def test_a_total_needs_the_currency_of_the_prices(tmp_path) -> None:
	ruta = tmp_path / "planilla.xlsx"
	pest = fabrica.pestanas(reservas_=[_fila()])
	pest["Reservas desde 15-nov"][2] = [c.replace(" (US$)", " ($U)") for c in fabrica.RES_CABECERA]
	fabrica.escribe_xlsx(ruta, pest)
	(error,) = _textos(planilla.lee(ruta, reservas=True).errores)
	assert "los totales están en UYU y los precios de «Temporadas y precios», en USD" in error


def test_one_code_on_two_platforms_is_an_error(tmp_path) -> None:
	p = _lee(tmp_path, [_fila(), _fila(Plataforma="Expedia", Llegada=D(2030, 2, 1), Salida=D(2030, 2, 3))])
	assert any("el código 4100000001 sale con Booking.com (fila 4) y con Expedia" in t for t in _textos(p.errores))


# --- el plan

def test_each_reservation_is_created_once_with_the_key_channex_uses(tmp_path) -> None:
	p = _lee(tmp_path)
	pl = plan.planea(p, _vacio(), PROP, hoy=HOY)
	assert pl.errores == []
	creadas = {a.valores["ref"]: a.valores for a in _reservas(pl, "crear")}
	assert len(creadas) == p.recuento()["reservas"] == 6
	assert {r: (v["ota_ref"], v["idempotency_key"], v["channel"], v["source"]) for r, v in creadas.items()} == {
		"4100000001": ("4100000001", None, "Booking.com", "OTA"),
		"4100000002": ("4100000002", None, "Booking.com", "OTA"),
		"EXP-INV-77-0": ("EXP-INV-77-0", None, "Expedia", "OTA"),
		"EXP-INV-77-1": ("EXP-INV-77-1", None, "Expedia", "OTA"),
		"HMINVENT04": ("HMINVENT04", None, "Airbnb", "OTA"),
		"T-0005": (None, "planilla:T-0005", None, "Phone"),
	}
	assert pl.recuento()["Reservation"] == {"crear": 6}


def test_a_second_run_creates_no_reservation(tmp_path) -> None:
	p = _lee(tmp_path)
	primera = plan.planea(p, _vacio(), PROP, hoy=HOY)
	cargadas = _cargado(primera)
	segunda = plan.planea(p, _con_tipos(primera, reservas_con_clave=cargadas, ocupadas=cargadas), PROP, hoy=HOY)
	assert segunda.errores == []
	assert [(a.op, a.nombre) for a in _reservas(segunda)] == [("igual", r["name"]) for r in cargadas]
	assert segunda.recuento()["Reservation"] == {"igual": 6}
	assert not [t for t in segunda.avisos if "en Kamra" in t]


def test_a_reservation_changed_in_kamra_is_left_alone_with_a_warning(tmp_path) -> None:
	p = _lee(tmp_path)
	primera = plan.planea(p, _vacio(), PROP, hoy=HOY)
	cargadas = _cargado(primera)
	cargadas[0].update(check_in_date=D(2030, 1, 4), check_out_date=D(2030, 1, 7))  # Channex la movió
	cargadas[1]["status"] = "Cancelled"  # Booking.com la canceló
	segunda = plan.planea(p, _con_tipos(primera, reservas_con_clave=cargadas, ocupadas=cargadas[:1] + cargadas[2:]),
	                      PROP, hoy=HOY)
	assert segunda.errores == []
	assert {a.op for a in _reservas(segunda)} == {"igual"}
	assert any("4100000001 (Booking.com, fila 4) ya está en Kamra (RES-2030-00001) con otros datos: la llegada y la "
	           "salida" in t for t in segunda.avisos)
	assert any("4100000002 (Booking.com, fila 5) está cancelada en Kamra (RES-2030-00002): la carga no la reabre" in t
	           for t in segunda.avisos)


def test_a_booking_kept_with_another_number_of_rooms_is_an_error(tmp_path) -> None:
	p = _lee(tmp_path)
	primera = plan.planea(p, _vacio(), PROP, hoy=HOY)
	cargadas = [r for r in _cargado(primera) if not r["ota_ref"] or not r["ota_ref"].startswith("EXP")]
	cargadas.append({**_cargado(primera)[2], "name": "RES-2030-00099", "ota_ref": "EXP-INV-77"})  # una sola habitación
	segunda = plan.planea(p, _con_tipos(primera, reservas_con_clave=cargadas), PROP, hoy=HOY)
	assert [t for t in segunda.errores if "EXP-INV-77" in t] == [
		f"La reserva EXP-INV-77-{n} (Expedia, fila {6 + n}): en Kamra ya está como EXP-INV-77 (RES-2030-00099), con otro "
		"número de habitaciones. Corrige la planilla o la reserva de Kamra para que digan lo mismo; la carga no la duplica."
		for n in (0, 1)
	]


def test_a_finished_reservation_is_skipped_and_one_in_progress_is_an_error(tmp_path) -> None:
	p = _lee(tmp_path)
	pl = plan.planea(p, _vacio(), PROP, hoy=D(2030, 1, 13))
	assert pl.errores == [
		"La reserva 4100000002 (Booking.com, fila 5) empezó el 12-01-2030: el huésped ya está en el hotel. Cárgala a "
		"mano en Kamra, con su check-in, y quítala de la planilla."
	]
	assert "1 reserva ya terminó (antes del 13-01-2030) y no se carga: 4100000001." in pl.avisos


def test_more_bookings_than_rooms_of_a_type_is_an_error_before_writing(tmp_path) -> None:
	filas = [_fila(Código=f"410000001{n}") for n in range(3)]  # 3 Dobles las mismas noches; hay 2
	pl = plan.planea(_lee(tmp_path, filas), _vacio(), PROP, hoy=HOY)
	assert pl.errores == [
		"La noche del 03-01-2030 no caben las reservas de Doble: hay 2 habitaciones y 3 reservas (0 ya en Kamra y 3 de "
		"la planilla: 4100000010, 4100000011, 4100000012). Corrígelo antes de cargar."
	]


def test_what_kamra_already_sells_counts_against_the_rooms(tmp_path) -> None:
	p = _lee(tmp_path, [_fila()])
	pl0 = plan.planea(p, _vacio(), PROP, hoy=HOY)
	ajenas = [{"name": f"RES-WEB-{n}", "status": "Confirmed", "room_type": f"{PROP}-DOBLE", "room": None,
	           "check_in_date": D(2030, 1, 5), "check_out_date": D(2030, 1, 6)} for n in (1, 2)]
	pl = plan.planea(p, _con_tipos(pl0, ocupadas=ajenas), PROP, hoy=HOY)
	assert pl.errores == [
		"La noche del 05-01-2030 no caben las reservas de Doble: hay 2 habitaciones y 3 reservas (2 ya en Kamra y 1 de "
		"la planilla: 4100000001). Corrígelo antes de cargar."
	]


def test_two_bookings_in_one_room_the_same_night_is_an_error(tmp_path) -> None:
	filas = [_fila(Habitación="801"), _fila(Código="4100000020", Habitación="801", Llegada=D(2030, 1, 5),
	                                         Salida=D(2030, 1, 8))]
	pl = plan.planea(_lee(tmp_path, filas), _vacio(), PROP, hoy=HOY)
	assert pl.errores == [
		"La habitación 801 tiene 2 reservas la noche del 05-01-2030: 4100000001, 4100000020. Deja en blanco la "
		"habitación de una (se asigna después en Kamra) o corrígelo antes de cargar."
	]


def test_cancelled_in_the_planilla_but_live_in_kamra_is_a_warning(tmp_path) -> None:
	p = _lee(tmp_path)
	primera = plan.planea(p, _vacio(), PROP, hoy=HOY)
	viva = {**_cargado(primera)[0], "name": "RES-2030-00042", "ota_ref": "4100000006"}
	pl = plan.planea(p, _con_tipos(primera, reservas_con_clave=[viva]), PROP, hoy=HOY)
	assert any("La reserva 4100000006 (Booking.com) está cancelada en la planilla y viva en Kamra (RES-2030-00042)" in t
	           for t in pl.avisos)


def test_the_report_lists_reservations_without_the_guests_data(tmp_path) -> None:
	p = _lee(tmp_path)
	texto = plan.planea(p, _vacio(), PROP, hoy=HOY).texto(aplicado=False)
	assert ("Reservas en la planilla: 6 (Airbnb 1, Booking.com 2, Directa 1, Expedia 2) · 1 cancelada (no se "
	        "carga)") in texto
	assert "Reservas: crearía 6" in texto
	assert "  + EXP-INV-77-1 (Expedia): Doble, 22-01-2030 → 25-01-2030, 2 adultos, total 1200 USD" in texto
	assert ("  + HMINVENT04 (Airbnb): Apartamento, habitación 806, 01-02-2030 → 08-02-2030, 4 adultos y 1 niño, al "
	        "precio de Kamra") in texto
	for r in p.reservas:
		assert r.huesped not in texto
		for dato in (r.telefono, r.correo):
			assert not dato or dato not in texto


def test_a_loaded_reservation_that_has_started_since_is_left_alone(tmp_path) -> None:
	p = _lee(tmp_path)
	primera = plan.planea(p, _vacio(), PROP, hoy=HOY)
	cargadas = _cargado(primera)
	segunda = plan.planea(p, _con_tipos(primera, reservas_con_clave=cargadas, ocupadas=cargadas), PROP,
	                      hoy=D(2030, 1, 13))  # la 4100000002 llegó el 12 y la 4100000001 ya se fue
	assert segunda.errores == []
	assert {a.op for a in _reservas(segunda)} == {"igual"}


def test_headers_go_to_the_column_that_says_it_best(tmp_path) -> None:
	# «Tipo de pago» antes que «Tipo de habitación» no se lleva el tipo, y un «Precio por noche» no es el total
	cabecera = ["Tipo de pago", "Nº de reserva", "Canal", "Nombre", "Fecha de llegada", "Fecha de salida",
	            "Tipo de habitación", "Precio por noche (US$)", "Adultos"]
	fila = ["Prepago", "4100000001", "Booking.com", "Huésped Inventado", D(2030, 1, 3), D(2030, 1, 6), "Doble", 800, 2]
	pest = fabrica.pestanas(reservas_=[fila])
	pest["Reservas desde 15-nov"][2] = cabecera
	ruta = tmp_path / "planilla.xlsx"
	fabrica.escribe_xlsx(ruta, pest)
	p = planilla.lee(ruta, reservas=True)
	assert p.errores == []
	(r,) = p.reservas
	assert (r.ref, r.plataforma, r.tipo, r.total, r.llegada) == ("4100000001", "Booking.com", "Doble", None, D(2030, 1, 3))
