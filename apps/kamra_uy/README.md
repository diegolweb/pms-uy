# Kamra UY

Localización uruguaya de [Kamra PMS](https://github.com/Kamra-PMS/kamra-pms) para el Hotel Roosevelt. Es una app de
Frappe v16 que se instala sobre `kamra` en el mismo sitio.

## Instalación

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO
bench --site <sitio> install-app kamra_uy
```

Requiere `kamra` instalada antes (`required_apps = ["kamra"]`).

## Tests

- `tests/unit/`: lógica pura y metadatos, sin frappe: `pytest tests/unit`.
- `kamra_uy/tests/`: tests de integración, dentro de bench: `bench --site <sitio> run-tests --app kamra_uy`.

## Licencia

AGPL-3.0 (ver `license.txt`), la misma que Kamra.
