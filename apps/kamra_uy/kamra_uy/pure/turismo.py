"""La Semana de Turismo y el aviso del IVA a residentes (HR-227, A6 de KICKOFF-HR-220, D-HR-40). Sin frappe.

La Semana de Turismo es la Semana Santa: de lunes al domingo de Pascua, que se calcula (algoritmo gregoriano anónimo,
«Computus»); no hace falta ninguna tabla. La alta temporada va del 16 de noviembre de un año al domingo de Pascua del
siguiente, ambos incluidos, y el aviso del IVA a residentes sale si ALGUNA NOCHE de la estancia cae dentro (D-HR-40:
manda sobre «si el check-in cae»; cuadra con lo que factura core noche a noche). Es la misma regla que
`avisoIvaResidentes` de web/src/booking/reglas.ts (HR-53), con los mismos vectores: 2026 → 30-mar a 5-abr,
2027 → 22 a 28-mar, 2028 → 10 a 16-abr. Si el contador la convierte en regla de IVA, la fuente pasa a core.
"""

from __future__ import annotations

from datetime import date, timedelta

# el primer día de la alta temporada de cada año (16 de noviembre)
INICIO_ALTA = (11, 16)


def domingo_de_pascua(anio: int) -> date:
    """El domingo de Pascua del año (calendario gregoriano)."""
    a, b, c = anio % 19, anio // 100, anio % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mes, dia = divmod(h + l - 7 * m + 114, 31)
    return date(anio, mes, dia + 1)


def semana_de_turismo(anio: int) -> tuple[date, date]:
    """(lunes, domingo de Pascua) de la Semana de Turismo del año."""
    domingo = domingo_de_pascua(anio)
    return domingo - timedelta(days=6), domingo


def en_alta_temporada(noche: date) -> bool:
    """Si la noche cae entre el 16 de noviembre y el domingo de Pascua siguiente, ambos incluidos."""
    return noche >= date(noche.year, *INICIO_ALTA) or noche <= domingo_de_pascua(noche.year)


def _fecha(valor) -> date:
    """Una fecha de calendario: `date` o «AAAA-MM-DD». ValueError/TypeError si no lo es."""
    if isinstance(valor, date):
        return valor
    if not isinstance(valor, str) or len(valor) != 10:
        raise ValueError(f"fecha no válida: {valor!r}")
    return date.fromisoformat(valor)


def aviso_iva_residentes(llegada, salida) -> bool:
    """Si alguna noche de la estancia (de la llegada a la noche anterior a la salida) cae en la alta temporada."""
    desde, hasta = _fecha(llegada), _fecha(salida)
    noche = desde
    while noche < hasta:
        if en_alta_temporada(noche):
            return True
        noche += timedelta(days=1)
    return False
