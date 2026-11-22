"""La marca del hotel en las superficies de Kamra que ve el huésped (HR-32).

- `aplica.aplicar`: lleva el paquete de marca a los ajustes de Kamra y de Frappe (el símbolo y el morado de la ficha
  del hotel, que pintan /kamra/book, el pre-check-in y la factura; los enlaces legales en el pie de la factura; el
  logo de los correos). Lo arranca deploy/prod/branding/aplica.sh con `bench execute`.
- `correo.envolver`, `correo.enlace` y `correo.boton`: el correo al huésped con la identidad y el pie de la web en su
  idioma. Los usa quien escribe el correo (HR-227, la confirmación de la reserva en línea).
- `marca`: la validación del paquete y la marca guardada.

Aquí no vive ningún dato del hotel (kamra_uy se publica como AGPL): el paquete, con el logo, la paleta y los textos
aprobados, vive en deploy/prod/branding/ (marca.json y activos/).
"""
