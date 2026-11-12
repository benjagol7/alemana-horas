import json
import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

# Consulta directa al calendario de la clínica (la misma que hace la página al
# cambiar de semana). No usa navegador ni RUT: solo el identificador del doctor.
API = ("https://reserva-api.clinicaalemana.cl/api/web/profesional/id/"
       "components/calendar/api/calendar")

# ---- Doctores a vigilar ----
# "professional_id" y "area_medica": salen de DevTools -> Network -> petición
#   "calendar" -> pestaña Payload (filter.professionalId y medicalAreaInternalId).
# "url": página del doctor, solo para el link del correo.
# "dias": avisa si hay alguna hora entre hoy y hoy + esa cantidad de días.
DOCTORES = [
    {
        "nombre": "Yalda Cecilia Lucero Alvarez",
        "url": "https://reserva.clinicaalemana.cl/profesional/yalda-cecilia-lucero-alvarez-6035113",
        "professional_id": "01936908-7608-738b-a2df-cd849c03a8a6",
        "area_medica": "22",
        "dias": 20,
    },
    # --- Falta el professional_id de cada una: pegarlo y quitar los # ---
    # {
    #     "nombre": "Maria Francisca Jaime Mendez",
    #     "url": "https://reserva.clinicaalemana.cl/profesional/maria-francisca-jaime-mendez-5829339",
    #     "professional_id": "PEGAR_AQUI",
    #     "area_medica": "22",
    #     "dias": 20,
    # },
    # {
    #     "nombre": "Lorena Marcela Rodriguez Gonzalez",
    #     "url": "https://reserva.clinicaalemana.cl/profesional/lorena-marcela-rodriguez-gonzalez-5173649",
    #     "professional_id": "PEGAR_AQUI",
    #     "area_medica": "22",
    #     "dias": 20,
    # },
    # {
    #     "nombre": "Maria Eugenia Arancibia Soto",
    #     "url": "https://reserva.clinicaalemana.cl/profesional/maria-eugenia-arancibia-soto-2602",
    #     "professional_id": "PEGAR_AQUI",
    #     "area_medica": "22",
    #     "dias": 20,
    # },
    # {
    #     "nombre": "Maria Cecilia Riutort Kreft",
    #     "url": "https://reserva.clinicaalemana.cl/profesional/maria-cecilia-riutort-kreft-5468057",
    #     "professional_id": "PEGAR_AQUI",
    #     "area_medica": "22",
    #     "dias": 20,
    # },
]

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def lunes_de(fecha):
    return fecha - timedelta(days=fecha.weekday())


def semanas_de_la_ventana(hoy, dias):
    """Lunes de cada semana que toca la ventana hoy .. hoy+dias."""
    limite = hoy + timedelta(days=dias)
    lunes = lunes_de(hoy)
    semanas = []
    while lunes <= limite:
        semanas.append(lunes)
        lunes += timedelta(days=7)
    return semanas


def consultar_semana(doc, lunes, api=API):
    """Pide al calendario los días con horas de una semana. Devuelve lista de fechas."""
    cuerpo = {
        "startDate": lunes.isoformat(),
        "extendSearch": False,
        "timeFrame": {"unit": "week"},
        "filter": {"professionalId": doc["professional_id"]},
        "medicalAreaInternalId": doc["area_medica"],
        "optionalFilter": [{"type": "services", "serviceIds": []}],
        "waitlistLatencyDays": 15,
    }
    r = requests.post(api, json=cuerpo, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} en la semana del {lunes.isoformat()}: {r.text[:300]!r}")
    try:
        datos = json.loads(r.text)
        return [date.fromisoformat(d["date"]) for d in datos["dates"]]
    except Exception:
        raise RuntimeError(f"respuesta inesperada en la semana del {lunes.isoformat()}: {r.text[:300]!r}")


def fechas_en_ventana(doc, hoy, api=API):
    """Todas las fechas con horas entre hoy y hoy + dias (inclusive), ordenadas."""
    limite = hoy + timedelta(days=doc["dias"])
    encontradas = set()
    for lunes in semanas_de_la_ventana(hoy, doc["dias"]):
        fechas = consultar_semana(doc, lunes, api)
        print(f"  Semana del {lunes.isoformat()}: {[f.isoformat() for f in fechas]}")
        encontradas.update(f for f in fechas if hoy <= f <= limite)
    return sorted(encontradas)


def fecha_legible(f):
    return f"{DIAS[f.weekday()]} {f.day:02d}/{f.month:02d}"


def enviar_correo(nombre, url, fechas):
    api_key = os.environ["RESEND_API_KEY"]
    destino = os.environ["EMAIL_TO"]

    lista = ", ".join(fecha_legible(f) for f in fechas)
    cuerpo = (f"¡Hay hora disponible para {nombre}!\n\n"
              f"Días con hora: {lista}\n\n"
              f"Reserva aquí:\n{url}")

    datos = {
        "from": "Horas Clinica Alemana <onboarding@resend.dev>",
        "to": [destino],
        "subject": f"¡Hora disponible! {nombre} - {fecha_legible(fechas[0])}",
        "text": cuerpo,
    }

    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}"},
        json=datos,
    )
    print(f"  Correo enviado ({nombre}):", r.status_code, r.text)
    r.raise_for_status()


def revisar_todos(doctores, hoy, enviar=enviar_correo, api=API):
    """Revisa cada doctor. Devuelve la cantidad de doctores que no se pudieron consultar."""
    errores = 0
    for doc in doctores:
        nombre = doc["nombre"]
        limite = hoy + timedelta(days=doc["dias"])
        print(f"== Revisando {nombre} ({hoy.isoformat()} a {limite.isoformat()}) ==")
        try:
            fechas = fechas_en_ventana(doc, hoy, api)
            if fechas:
                print(f"  Con hora en la ventana: {[f.isoformat() for f in fechas]}")
                enviar(nombre, doc["url"], fechas)
            else:
                print("  Sin horas en la ventana. Sin aviso.")
        except Exception as e:
            errores += 1
            print(f"  ERROR revisando {nombre}: {e}")
    return errores


if __name__ == "__main__":
    hoy = datetime.now(ZoneInfo("America/Santiago")).date()
    errores = revisar_todos(DOCTORES, hoy)
    # Si algún doctor no se pudo consultar, la corrida queda en rojo para que se note.
    if errores:
        sys.exit(1)
