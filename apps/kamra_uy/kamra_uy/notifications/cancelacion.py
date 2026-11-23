"""La cancelación del correo de confirmación (HR-291), armada con los campos de la Property. Sin frappe.

Dice lo que Kamra cobra (kamra.api._cancellation_terms, la vista previa de la cancelación, y kamra.no_show), con los mismos valores:

- `free_cancel_days` (N) y `cancellation_fee`: Kamra cobra solo si al cancelar quedan MENOS de N días para la llegada.
  Así que es gratis «hasta N días antes» y, después, lo de `cancellation_fee`: «Full Stay» (el 100 % de la estadía) o
  «First Night» (la primera noche). Con N = 0 no se cobra ni el mismo día de la llegada, y con «None» no se cobra
  nunca: en los dos casos, gratis «hasta el día de la llegada».
- `no_show_charge`: lo que se cobra si no llega («Full Stay», «First Night» o «None», nada).
- Con tramos (`Property.cancellation_tiers`), Kamra cobra por ellos y no por N: la ventana sería falsa, así que no se
  dice (queda lo de no llegar). Los tramos en el correo son HR-301.
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


def parrafos(idioma: str, dias, cargo, no_show, *, tramos: bool = False,
             no_reembolsable: bool = False) -> tuple[str, list[str]]:
	"""(titular, párrafos) de la cancelación en `idioma`: el de cancelar y el de no llegar, como condiciones.md."""
	t = CANCELACION[idioma]
	n = int(dias or 0)
	if n < 0:
		raise ValueError(f"días de cancelación gratis negativos: {n}")
	despues, sin_llegar = _clave(cargo), _clave(no_show)

	out = []
	if no_reembolsable:
		out.append(t["no_reembolsable"])
	elif not tramos:
		if n == 0 or despues is None:
			out.append(t["gratis_hasta_la_llegada"])
		else:
			plazo = (t["dia"] if n == 1 else t["dias"]).format(n=n)
			out.append(f"{t['gratis_hasta'].format(dias=plazo)} {t['despues_' + despues]}")
	out.append(t[f"no_show_{sin_llegar or 'none'}"])
	return t["titulo"], out
