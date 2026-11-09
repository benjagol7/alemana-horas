import os
import re
import sys
import time
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
    # --- En pausa mientras probamos con una sola doctora (quitar los # para reactivar) ---
    # {
    #     "nombre": "Lorena Marcela Rodriguez Gonzalez",
    #     "url": "https://reserva.clinicaalemana.cl/profesional/lorena-marcela-rodriguez-gonzalez-5173649",
    #     "dias": 20,
    # },
    # {
    #     "nombre": "Yalda Cecilia Lucero Alvarez",
    #     "url": "https://reserva.clinicaalemana.cl/profesional/yalda-cecilia-lucero-alvarez-6035113",
    #     "dias": 20,
    # },
    # {
    #     "nombre": "Maria Eugenia Arancibia Soto",
    #     "url": "https://reserva.clinicaalemana.cl/profesional/maria-eugenia-arancibia-soto-2602",
    #     "dias": 20,
    # },
    # {
    #     "nombre": "Maria Cecilia Riutort Kreft",
    #     "url": "https://reserva.clinicaalemana.cl/profesional/maria-cecilia-riutort-kreft-5468057",
    #     "dias": 20,
    # },
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

# Palabras que delatan una página de bloqueo o verificación anti-bots
PISTAS_BLOQUEO = [
    "captcha", "challenge", "access denied", "forbidden", "too many requests",
    "blocked", "awswaf", "cloudflare", "incapsula", "akamai", "unusual traffic",
]


def sin_tildes(s):
    return (s.lower().replace("á", "a").replace("é", "e").replace("í", "i")
            .replace("ó", "o").replace("ú", "u"))


def ocultar_rut(texto, rut):
    """Tapa el RUT en cualquier formato antes de escribir algo en el log (repo público)."""
    limpio = re.sub(r"[^0-9kK]", "", rut)
    if len(limpio) < 2:
        return texto
    digitos, dv = limpio[:-1], limpio[-1]
    patron = r"\.?".join(re.escape(d) for d in digitos) + r"\s*-?\s*" + re.escape(dv)
    return re.sub(patron, "[RUT]", texto, flags=re.IGNORECASE)


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


def texto_visible(pagina):
    try:
        return pagina.inner_text("body")
    except Exception:
        return ""


def esperar_red(pagina, ms=15000):
    try:
        pagina.wait_for_load_state("networkidle", timeout=ms)
    except Exception:
        pass


def ingresar_rut(pagina, rut):
    campo = pagina.locator(
        "input:not([type='radio']):not([type='checkbox']):not([type='hidden'])"
    ).first
    campo.click()
    campo.press_sequentially(rut, delay=80)
    pagina.get_by_role("button", name="Ingresar", exact=True).click()


def abrir(pagina, url, rut, recargar=False):
    """Abre la página del doctor (ingresando el RUT si el sitio lo pide).

    Devuelve el código HTTP de la respuesta.
    """
    if recargar:
        resp = pagina.reload(wait_until="load", timeout=60000)
    else:
        resp = pagina.goto(url, wait_until="load", timeout=60000)
    esperar_red(pagina)
    estado = resp.status if resp else None

    if "/paciente" in pagina.url:
        ingresar_rut(pagina, rut)
        try:
            pagina.wait_for_url(lambda u: "/paciente" not in u, timeout=30000)
        except Exception:
            raise RuntimeError("el sitio no aceptó el RUT (sigue en la pantalla de ingreso)")
        esperar_red(pagina)
    return estado


def esperar_contenido(pagina, hoy, segundos=25):
    """Espera hasta que la página muestre un día con horas (o termine de cargar sin ninguna)."""
    fin = time.monotonic() + segundos
    cargada_desde = None
    while True:
        texto = texto_visible(pagina)
        if proxima_hora(texto, hoy):
            return texto
        ahora = time.monotonic()
        if "Disponibilidad cargada" in texto:
            cargada_desde = cargada_desde or ahora
            if ahora - cargada_desde >= 4:
                return texto
        if ahora >= fin:
            return texto
        pagina.wait_for_timeout(1000)


def resumen_pagina(pagina, estado, rut):
    """Datos seguros para diagnosticar una página vacía (nunca el HTML completo)."""
    try:
        html = pagina.content()
    except Exception:
        html = ""
    try:
        titulo = pagina.title()
    except Exception:
        titulo = ""
    bajo = html.lower()
    pistas = [p for p in PISTAS_BLOQUEO if p in bajo]
    return ocultar_rut(
        f"HTTP {estado} | URL {pagina.url} | título '{titulo}' | "
        f"HTML de {len(html)} caracteres | pistas de bloqueo: {pistas or 'ninguna'}",
        rut,
    )


def leer_pagina(pagina, url, rut, hoy):
    estado = abrir(pagina, url, rut)
    texto = esperar_contenido(pagina, hoy)

    if not texto.strip():
        print("  Página vacía: " + resumen_pagina(pagina, estado, rut))
        print("  Reintentando en 10 segundos...")
        pagina.wait_for_timeout(10000)
        estado = abrir(pagina, url, rut, recargar=True)
        texto = esperar_contenido(pagina, hoy)

    if not texto.strip():
        raise RuntimeError("la página llegó vacía dos veces: " + resumen_pagina(pagina, estado, rut))

    print(f"  HTTP {estado} | {ocultar_rut(pagina.url, rut)}")
    return texto


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


def revisar_todos(pagina, doctores, rut, hoy, enviar=enviar_correo):
    """Revisa cada doctor. Devuelve la cantidad de doctores que no se pudieron leer."""
    errores = 0
    for doc in doctores:
        nombre = doc["nombre"]
        print(f"== Revisando {nombre} ==")
        try:
            texto = leer_pagina(pagina, doc["url"], rut, hoy)
            resultado = proxima_hora(texto, hoy)

            if resultado is None:
                print("  La página cargó pero no muestra ninguna hora. Texto visto:")
                print("  " + ocultar_rut(texto[:1500], rut).replace("\n", " | "))
                continue

            fecha, detalle = resultado
            print(f"  Próxima hora: {fecha.isoformat()} | {detalle}")

            limite = hoy + timedelta(days=doc["dias"])
            if en_ventana(fecha, hoy, doc["dias"]):
                enviar(nombre, doc["url"], fecha, detalle)
            else:
                print(f"  Fuera de la ventana ({hoy.isoformat()} a {limite.isoformat()}). Sin aviso.")
        except Exception as e:
            errores += 1
            print(f"  ERROR revisando {nombre}: {ocultar_rut(str(e), rut)}")
    return errores


if __name__ == "__main__":
    rut = os.environ["RUT_PACIENTE"].strip()
    hoy = datetime.now(ZoneInfo("America/Santiago")).date()

    with sync_playwright() as p:
        navegador = p.chromium.launch()
        pagina = navegador.new_page(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
        errores = revisar_todos(pagina, DOCTORES, rut, hoy)
        navegador.close()

    # Si algún doctor no se pudo leer, la corrida queda en rojo para que se note.
    if errores:
        sys.exit(1)
