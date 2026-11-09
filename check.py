import os
import re
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright

# ---- Doctores a vigilar (sitio nuevo: reserva.clinicaalemana.cl) ----
# "dias": avisa si la PRÓXIMA hora disponible del doctor cae entre hoy y
# hoy + esa cantidad de días (inclusive). La ventana se mueve sola cada día.
DOCTORES = [
    {
        "nombre": "Maria Francisca Jaime Mendez",
        "url": "https://reserva.clinicaalemana.cl/profesional/maria-francisca-jaime-mendez-5829339",
        "dias": 20,
    },
    {
        "nombre": "Lorena Marcela Rodriguez Gonzalez",
        "url": "https://reserva.clinicaalemana.cl/profesional/lorena-marcela-rodriguez-gonzalez-5173649",
        "dias": 20,
    },
    {
        "nombre": "Yalda Cecilia Lucero Alvarez",
        "url": "https://reserva.clinicaalemana.cl/profesional/yalda-cecilia-lucero-alvarez-6035113",
        "dias": 20,
    },
    {
        "nombre": "Maria Eugenia Arancibia Soto",
        "url": "https://reserva.clinicaalemana.cl/profesional/maria-eugenia-arancibia-soto-2602",
        "dias": 20,
    },
    {
        "nombre": "Maria Cecilia Riutort Kreft",
        "url": "https://reserva.clinicaalemana.cl/profesional/maria-cecilia-riutort-kreft-5468057",
        "dias": 7,
    },
]

MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}
DIAS = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]

# Encabezado del día con horas, por ejemplo "Lunes, 21 de Diciembre"
PATRON_DIA = re.compile(
    r"(lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo),?\s+"
    r"(\d{1,2})\s+de\s+([a-z]+)",
    re.IGNORECASE,
)
PATRON_HORA = re.compile(r"\b\d{1,2}:\d{2}\b")


def sin_tildes(s):
    return (s.lower().replace("á", "a").replace("é", "e").replace("í", "i")
            .replace("ó", "o").replace("ú", "u"))


def proxima_hora(texto, hoy):
    """Lee del texto de la página el primer día con horas.

    Devuelve (fecha, detalle) o None si la página no muestra ninguna hora.
    """
    for m in PATRON_DIA.finditer(texto):
        dia_semana = DIAS.index(sin_tildes(m.group(1)))
        dia = int(m.group(2))
        mes = MESES.get(sin_tildes(m.group(3)))
        if mes is None:
            continue

        # El encabezado no trae año: usamos el que calza con el día de la semana.
        fecha = None
        for anio in (hoy.year, hoy.year + 1):
            try:
                candidata = date(anio, mes, dia)
            except ValueError:
                continue
            if candidata.weekday() == dia_semana:
                fecha = candidata
                break
        if fecha is None:
            continue

        resto = texto[m.end():].split("©")[0]
        if not PATRON_HORA.search(resto):
            continue  # hay encabezado de día pero sin horas listadas

        lineas = [l.strip() for l in resto.splitlines() if l.strip()]
        detalle = " · ".join(lineas[:15])
        return fecha, detalle
    return None


def en_ventana(fecha, hoy, dias):
    return hoy <= fecha <= hoy + timedelta(days=dias)


def ingresar_rut(pagina, rut):
    campo = pagina.locator(
        "input:not([type='radio']):not([type='checkbox']):not([type='hidden'])"
    ).first
    campo.click()
    campo.press_sequentially(rut, delay=80)
    pagina.get_by_role("button", name="Ingresar", exact=True).click()
    pagina.wait_for_load_state("networkidle", timeout=60000)


def leer_pagina(pagina, url, rut):
    pagina.goto(url, wait_until="networkidle", timeout=60000)
    if "/paciente" in pagina.url:
        ingresar_rut(pagina, rut)
    if "/paciente" in pagina.url:
        raise RuntimeError("el sitio no aceptó el RUT (sigue en la pantalla de ingreso)")

    # Esperamos a que el sitio termine de cargar la disponibilidad
    try:
        pagina.wait_for_function(
            "document.body.innerText.includes('Disponibilidad cargada')",
            timeout=20000,
        )
    except Exception:
        pass
    pagina.wait_for_timeout(3000)
    return pagina.inner_text("body")


def enviar_correo(nombre, url, fecha, detalle):
    api_key = os.environ["RESEND_API_KEY"]
    destino = os.environ["EMAIL_TO"]

    fecha_txt = f"{DIAS[fecha.weekday()]} {fecha.day:02d}/{fecha.month:02d}/{fecha.year}"
    cuerpo = (f"¡Hay hora disponible para {nombre}!\n\n"
              f"Próxima hora: {fecha_txt}\n"
              f"{detalle}\n\n"
              f"Reserva aquí:\n{url}")

    datos = {
        "from": "Horas Clinica Alemana <onboarding@resend.dev>",
        "to": [destino],
        "subject": f"¡Hora disponible! {nombre} - {fecha.day:02d}/{fecha.month:02d}",
        "text": cuerpo,
    }

    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}"},
        json=datos,
    )
    print(f"  Correo enviado ({nombre}):", r.status_code, r.text)
    r.raise_for_status()


if __name__ == "__main__":
    rut = os.environ["RUT_PACIENTE"].strip()
    hoy = datetime.now(ZoneInfo("America/Santiago")).date()
    errores = 0

    with sync_playwright() as p:
        navegador = p.chromium.launch()
        pagina = navegador.new_page(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )

        for doc in DOCTORES:
            nombre = doc["nombre"]
            print(f"== Revisando {nombre} ==")
            try:
                texto = leer_pagina(pagina, doc["url"], rut)
                resultado = proxima_hora(texto, hoy)

                if resultado is None:
                    print("  No se detectó ninguna hora en la página. Texto visto:")
                    print("  " + texto[:1500].replace("\n", " | "))
                    continue

                fecha, detalle = resultado
                print(f"  Próxima hora: {fecha.isoformat()} | {detalle}")

                limite = hoy + timedelta(days=doc["dias"])
                if en_ventana(fecha, hoy, doc["dias"]):
                    enviar_correo(nombre, doc["url"], fecha, detalle)
                else:
                    print(f"  Fuera de la ventana ({hoy.isoformat()} a {limite.isoformat()}). Sin aviso.")
            except Exception as e:
                errores += 1
                print(f"  ERROR revisando {nombre}: {e}")

        navegador.close()

    if errores:
        sys.exit(1)
