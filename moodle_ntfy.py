"""
Avisos de tareas de Moodle al celular vía ntfy.

- Lee el calendario exportado (iCal) de Moodle.
- Avisa cuando aparece una tarea nueva.
- Recuerda 24 h y 3 h antes de la fecha de entrega.

Configuración por variables de entorno:
  MOODLE_ICS_URL   URL del calendario exportado de Moodle (contiene tu token: no la compartas)
  NTFY_TOPIC       Nombre del tema de ntfy (usa algo largo y difícil de adivinar)
  NTFY_SERVER      Opcional. Por defecto https://ntfy.sh
  TZ_LOCAL         Opcional. Zona horaria para mostrar fechas. Por defecto America/Guayaquil
"""
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from icalendar import Calendar

ICS_URL = os.environ["MOODLE_ICS_URL"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
TZ = ZoneInfo(os.environ.get("TZ_LOCAL", "America/Guayaquil"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))

# Recordatorios: (clave, horas antes, prioridad ntfy 1-5)
RECORDATORIOS = [
    ("24h", 24, 3),
    ("3h", 3, 4),
]


def a_datetime(valor):
    """Convierte DTSTART (date o datetime) a datetime con zona horaria."""
    if isinstance(valor, datetime):
        return valor if valor.tzinfo else valor.replace(tzinfo=TZ)
    if isinstance(valor, date):  # evento de día completo
        return datetime(valor.year, valor.month, valor.day, 23, 59, tzinfo=TZ)
    raise ValueError(f"Fecha no reconocida: {valor!r}")


def cargar_estado():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return None  # None = primera ejecución


def guardar_estado(estado):
    STATE_FILE.write_text(
        json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def notificar(titulo, mensaje, prioridad=3, etiquetas=None):
    # Se usa el formato JSON de ntfy para soportar tildes y emojis sin problemas
    r = requests.post(
        NTFY_SERVER,
        json={
            "topic": NTFY_TOPIC,
            "title": titulo,
            "message": mensaje,
            "priority": prioridad,
            "tags": etiquetas or ["books"],
        },
        timeout=20,
    )
    r.raise_for_status()


def obtener_eventos():
    r = requests.get(ICS_URL, timeout=30)
    r.raise_for_status()
    cal = Calendar.from_ical(r.content)
    eventos = []
    for ev in cal.walk("VEVENT"):
        eventos.append(
            {
                "uid": str(ev.get("UID")),
                "titulo": str(ev.get("SUMMARY", "Sin título")),
                "curso": str(ev.get("CATEGORIES").to_ical().decode())
                if ev.get("CATEGORIES")
                else "",
                "fecha": a_datetime(ev.get("DTSTART").dt),
            }
        )
    return eventos


def formato_fecha(dt):
    return dt.astimezone(TZ).strftime("%a %d/%m %H:%M")


def main():
    ahora = datetime.now(timezone.utc)
    eventos = [e for e in obtener_eventos() if e["fecha"] > ahora]
    eventos.sort(key=lambda e: e["fecha"])

    estado = cargar_estado()
    primera_vez = estado is None
    estado = estado or {}

    if primera_vez:
        # Evita inundarte con todo lo existente: marca todo como visto y manda un resumen
        for e in eventos:
            estado[e["uid"]] = {"nuevo": True, "24h": False, "3h": False}
        resumen = "\n".join(
            f"• {e['titulo']} ({formato_fecha(e['fecha'])})" for e in eventos[:8]
        )
        notificar(
            f"Avisos activados: {len(eventos)} pendientes",
            resumen or "No hay tareas próximas.",
            etiquetas=["white_check_mark"],
        )
        guardar_estado(estado)
        return

    for e in eventos:
        info = estado.setdefault(
            e["uid"], {"nuevo": False, "24h": False, "3h": False}
        )

        if not info["nuevo"]:
            notificar(
                "Nueva tarea en Moodle",
                f"{e['titulo']}\nEntrega: {formato_fecha(e['fecha'])}",
                prioridad=3,
                etiquetas=["new"],
            )
            info["nuevo"] = True

        faltan = e["fecha"] - ahora
        for clave, horas, prioridad in RECORDATORIOS:
            if not info[clave] and faltan <= timedelta(hours=horas):
                notificar(
                    f"Entrega en menos de {horas} h",
                    f"{e['titulo']}\nVence: {formato_fecha(e['fecha'])}",
                    prioridad=prioridad,
                    etiquetas=["alarm_clock"],
                )
                info[clave] = True
                # Si ya pasó el umbral de 24h y también el de 3h, no mandes ambos
                if clave == "24h" and faltan <= timedelta(hours=3):
                    info["3h"] = False

    # Limpia del estado los eventos que ya pasaron
    uids_vigentes = {e["uid"] for e in eventos}
    estado = {k: v for k, v in estado.items() if k in uids_vigentes}
    guardar_estado(estado)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # que el fallo se vea en los logs de Actions
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
