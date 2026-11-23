"""La cancelación del correo de confirmación (HR-291), armada con los campos de la Property. Sin frappe.

Dice lo que Kamra cobra (kamra.api._cancellation_terms, la vista previa de la cancelación, y kamra.no_show), con los mismos valores:

- `free_cancel_days` (N) y `cancellation_fee`: Kamra cobra solo si al cancelar quedan MENOS de N días para la llegada.
  Así que es gratis «hasta N días antes» y, después, lo de `cancellation_fee`: «Full Stay» (el 100 % de la estadía) o
  «First Night» (la primera noche). Con N = 0 no se cobra ni el mismo día de la llegada, y con «None» no se cobra
  nunca: en los dos casos, gratis «hasta el día de la llegada».
- `no_show_charge`: lo que se cobra si no llega («Full Stay», «First Night» o «None», nada).
- Con tramos (`Property.cancellation_tiers`), Kamra cobra por ellos y no por N (HR-301): el correo dice cada banda, con
  lo que cobra `kamra.api.cancellation_tier_for` cada día que falte para la llegada. Manda la banda de `days_before` más
  alto que no pase de los días que faltan y, antes que todas, la última; con `charge_pct` <= 0 no se cobra
  (`inside_window = pct > 0`), y si no, el `charge_pct` % de la estadía (`percent_fee`). Dos bandas seguidas que cobran
  lo mismo se dicen juntas, y con una sola (o todas iguales) es lo mismo en cualquier momento.
- Si alguna noche cae en una temporada o un paquete `non_refundable`, Kamra cobra el 100 % al cancelar, con tramos o
  sin ellos: lo dice en vez de la ventana.

Con los de la planilla del hotel (5 días, «Full Stay» y «Full Stay») dice lo mismo que web/content/*/condiciones.md.
Los textos están en texts.CANCELACION; aquí no va ninguna cifra.
"""

from __future__ import annotations

from kamra_uy.notifications.texts import CANCELACION

CARGOS = {"None": None, "First Night": "first_night", "Full Stay": "full_stay"}  # las opciones de Kamra


def _clave(valor) -> str | None:
	"""La clave de los textos para un cargo de Kamra; vacío = «None», como lo lee Kamra (`or "None"`)."""
	valor = valor or "None"
	if valor not in CARGOS:
		raise ValueError(f"cargo de cancelación desconocido: {valor!r}")
	return CARGOS[valor]


def _cargo_del_dia(filas, dias: int) -> float:
	"""El % que cobra Kamra al cancelar con `dias` para la llegada (el bucle de kamra.api.cancellation_tier_for, con
	`filas` ya en su orden: `days_before` de mayor a menor). Con <= 0 no cobra nada: 0."""
	for t in filas:
		if dias >= int(t["days_before"] or 0):
			return max(float(t["charge_pct"] or 0), 0.0)
	return max(float(filas[-1]["charge_pct"] or 0), 0.0)


def rangos(filas) -> list[tuple[int, int | None, float]]:
	"""Los tramos de la Property como los vive el huésped: (desde, hasta, %) por días que faltan para la llegada, de 0
	en adelante; el último, sin `hasta`. `filas`: las de Property.cancellation_tiers (`days_before`, `charge_pct`), en
	el orden en que Kamra las lee; se ordenan como Kamra (`days_before` de mayor a menor, estable para los empates)."""
	filas = sorted(filas, key=lambda t: int(t["days_before"] or 0), reverse=True)
	if not filas:
		return []
	out: list[list] = []
	for d in range(max(int(filas[0]["days_before"] or 0), 0) + 1):  # desde la banda más alta, ya no cambia
		pct = _cargo_del_dia(filas, d)
		if out and out[-1][2] == pct:
			out[-1][1] = d
		else:
			out.append([d, d, pct])
	out[-1][1] = None
	return [tuple(r) for r in out]


def _plazo(t: dict, n: int) -> str:
	return (t["dia"] if n == 1 else t["dias"]).format(n=n)


def _cobro(t: dict, idioma: str, pct: float) -> str:
	if pct <= 0:
		return t["cobro_nada"]
	cifra = f"{pct:g}" if idioma == "en" else f"{pct:g}".replace(".", ",")
	return t["cobro_total" if pct >= 100 else "cobro_parte"].format(pct=f"{cifra}%")


def _bandas(t: dict, idioma: str, filas) -> list[str]:
	r = rangos(filas)
	if len(r) == 1:
		return [t["tramo_siempre"].format(cobro=_cobro(t, idioma, r[0][2]))]
	out = [t["tramos_intro"]]
	for desde, hasta, pct in reversed(r):  # de lejos a cerca de la llegada
		cobro = _cobro(t, idioma, pct)
		if hasta is None:
			out.append(t["tramo_desde"].format(dias=_plazo(t, desde), cobro=cobro))
		elif desde == 0 and hasta == 0:
			out.append(t["tramo_llegada"].format(cobro=cobro))
		elif desde == 0:
			out.append(t["tramo_menos"].format(dias=_plazo(t, hasta + 1), cobro=cobro))
		elif desde == hasta:
			out.append(t["tramo_exacto"].format(dias=_plazo(t, desde), cobro=cobro))
		else:
			out.append(t["tramo_entre"].format(desde=desde, hasta=_plazo(t, hasta), cobro=cobro))
	return out


def parrafos(idioma: str, dias, cargo, no_show, *, tramos=(),
             no_reembolsable: bool = False) -> tuple[str, list[str]]:
	"""(titular, párrafos) de la cancelación en `idioma`: el de cancelar (o sus tramos) y el de no llegar, como
	condiciones.md. `tramos`: las filas de Property.cancellation_tiers; vacío, la ventana de `free_cancel_days`."""
	t = CANCELACION[idioma]
	n = int(dias or 0)
	if n < 0:
		raise ValueError(f"días de cancelación gratis negativos: {n}")
	despues, sin_llegar = _clave(cargo), _clave(no_show)

	out = []
	if no_reembolsable:
		out.append(t["no_reembolsable"])
	elif tramos:
		out += _bandas(t, idioma, tramos)
	else:
		if n == 0 or despues is None:
			out.append(t["gratis_hasta_la_llegada"])
		else:
			plazo = (t["dia"] if n == 1 else t["dias"]).format(n=n)
			out.append(f"{t['gratis_hasta'].format(dias=plazo)} {t['despues_' + despues]}")
	out.append(t[f"no_show_{sin_llegar or 'none'}"])
	return t["titulo"], out
