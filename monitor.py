\
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

STATE_PATH = Path("state.json")
SCREENSHOT_DIR = Path("screenshots")

INFO_URL = "https://www.gub.uy/ministerio-salud-publica/vacunacion-meningococo"

REGIONS = {
    "Sur": "https://sae.msp.gub.uy/sae/agendarReserva/Paso1.xhtml?a=4&e=52",
    "Este": "https://sae.msp.gub.uy/sae/agendarReserva/Paso1.xhtml?a=3&e=52",
    "Oeste": "https://sae.msp.gub.uy/sae/agendarReserva/Paso1.xhtml?a=2&e=52",
    "Norte": "https://sae.msp.gub.uy/sae/agendarReserva/Paso1.xhtml?a=1&e=52",
}

NO_SLOTS_PATTERNS = (
    "no hay cupos disponibles",
    "a la brevedad se añadirán cupos",
)

SLOT_DATE_RE = re.compile(
    r"(lunes|martes|miércoles|miercoles|jueves|viernes|sábado|sabado|domingo)"
    r"\s+\d{1,2}\s+de\s+[a-záéíóúñ]+\s*-\s*\d{1,2}:\d{2}\s*h",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Slot:
    region: str
    office: str
    when: str
    url: str

    @property
    def key(self) -> str:
        raw = f"{self.region}|{self.office}|{self.when}|{self.url}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def normalize(text: str) -> str:
    return " ".join(text.split()).strip()


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"seen": {}, "last_page_fingerprint": None}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logging.warning("No se pudo leer state.json; se inicia un estado nuevo.")
        return {"seen": {}, "last_page_fingerprint": None}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def send_telegram(message: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        raise RuntimeError(
            "Faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID en los secretos de GitHub."
        )

    # Telegram admite como máximo 4096 caracteres por mensaje.
    max_length = 3500
    parts = []

    while message:
        if len(message) <= max_length:
            parts.append(message)
            break

        split_at = message.rfind("\n\n", 0, max_length)
        if split_at == -1:
            split_at = max_length

        parts.append(message[:split_at])
        message = message[split_at:].lstrip()

    for part in parts:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": part,
                "disable_web_page_preview": True,
            },
            timeout=30,
        )

        if not response.ok:
            raise RuntimeError(
                f"Telegram devolvió {response.status_code}: {response.text}"
            )

def test_telegram() -> None:
    send_telegram(
        "✅ Prueba correcta: el monitor de cupos del MSP puede enviarte mensajes."
    )


def official_page_fingerprint() -> tuple[str, str]:
    response = requests.get(
        INFO_URL,
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0 monitor-cupos-msp/1.0"},
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    main = soup.find("main") or soup
    text = normalize(main.get_text(" ", strip=True))
    fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return fingerprint, text


def visible_text(locator: Locator) -> str:
    try:
        return normalize(locator.inner_text(timeout=1500))
    except Exception:
        return ""


def find_office_controls(page: Page) -> list[Locator]:
    # SAE usa controles de selección de oficina. Probamos radios y elementos
    # accesibles con role=radio; se deduplican por cantidad/orden.
    radios = page.locator('input[type="radio"]:visible')
    if radios.count() > 0:
        return [radios.nth(i) for i in range(radios.count())]

    role_radios = page.get_by_role("radio")
    return [role_radios.nth(i) for i in range(role_radios.count())]


def label_for_control(page: Page, control: Locator, index: int) -> str:
    control_id = control.get_attribute("id")
    if control_id:
        label = page.locator(f'label[for="{control_id}"]')
        if label.count():
            text = visible_text(label.first)
            if text:
                return text

    aria = control.get_attribute("aria-label")
    if aria:
        return normalize(aria)

    # Respaldo: toma texto cercano del contenedor.
    try:
        parent = control.locator("xpath=..")
        text = visible_text(parent)
        if text:
            return text
    except Exception:
        pass

    return f"Vacunatorio {index + 1}"


def extract_slots_from_page(page: Page, region: str, office: str, url: str) -> list[Slot]:
    body_text = normalize(page.locator("body").inner_text())
    matches = [normalize(m.group(0)) for m in SLOT_DATE_RE.finditer(body_text)]
    return [Slot(region=region, office=office, when=m, url=url) for m in dict.fromkeys(matches)]


def monitor_region(page: Page, region: str, url: str) -> list[Slot]:
    logging.info("Revisando región %s", region)
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(1500)

    controls = find_office_controls(page)
    slots: list[Slot] = []

    if not controls:
        # El SAE a veces deja un vacunatorio seleccionado o expone turnos sin
        # controles detectables. En ese caso se registra la disponibilidad visible.
        return extract_slots_from_page(page, region, "Vacunatorio mostrado", url)

    for index in range(len(controls)):
        # La página puede re-renderizarse después de cada selección, por eso se
        # vuelve a resolver el conjunto de controles.
        controls_now = find_office_controls(page)
        if index >= len(controls_now):
            break

        control = controls_now[index]
        office = label_for_control(page, control, index)

        try:
            control.check(force=True, timeout=5_000)
        except Exception:
            try:
                control.click(force=True, timeout=5_000)
            except Exception as exc:
                logging.warning("No se pudo seleccionar %s / %s: %s", region, office, exc)
                continue

        page.wait_for_timeout(900)
        found = extract_slots_from_page(page, region, office, url)
        slots.extend(found)

    # Deduplicación exacta.
    unique = {slot.key: slot for slot in slots}
    return list(unique.values())


def format_summary(slots: Iterable[Slot]) -> str:
    slots = list(slots)
    counts = {region: 0 for region in REGIONS}

    for slot in slots:
        counts[slot.region] = counts.get(slot.region, 0) + 1

    lines = [
        "🚨 MSP — Nuevos cupos detectados",
        "",
        f"Total: {len(slots)} horarios nuevos",
        "",
    ]

    for region in ("Sur", "Este", "Oeste", "Norte"):
        count = counts.get(region, 0)
        if count:
            lines.append(f"📍 {region}: {count} horario{'s' if count != 1 else ''}")
        else:
            lines.append(f"⚪ {region}: sin cupos nuevos")

    return "\n".join(lines)


def format_region_alert(region: str, slots: Iterable[Slot]) -> str:
    slots = list(slots)

    lines = [
        f"📍 REGIÓN {region.upper()}",
        "",
    ]

    for slot in slots:
        lines.extend(
            [
                f"Vacunatorio: {slot.office}",
                f"Turno: {slot.when}",
                "",
            ]
        )

    lines.extend(
        [
            f"Abrir agenda: {REGIONS[region]}",
            "",
            "Reservá de inmediato: la disponibilidad puede cambiar.",
        ]
    )

    return "\n".join(lines)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if "--test-telegram" in sys.argv:
        test_telegram()
        return 0

    state = load_state()
    seen: dict[str, dict] = state.setdefault("seen", {})
    all_slots: list[Slot] = []

    SCREENSHOT_DIR.mkdir(exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            locale="es-UY",
            timezone_id="America/Montevideo",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/140 Safari/537.36"
            ),
        )
        page = context.new_page()

        for region, url in REGIONS.items():
            try:
                all_slots.extend(monitor_region(page, region, url))
            except PlaywrightTimeoutError:
                logging.exception("Tiempo agotado al revisar %s", region)
                try:
                    page.screenshot(
                        path=str(SCREENSHOT_DIR / f"error-{region.lower()}.png"),
                        full_page=True,
                    )
                except Exception:
                    pass
            except Exception:
                logging.exception("Error al revisar %s", region)

        browser.close()

    now = datetime.now(timezone.utc).isoformat()
    new_slots = [slot for slot in all_slots if slot.key not in seen]

    for slot in all_slots:
        seen[slot.key] = {
            "region": slot.region,
            "office": slot.office,
            "when": slot.when,
            "url": slot.url,
            "first_seen_utc": seen.get(slot.key, {}).get("first_seen_utc", now),
            "last_seen_utc": now,
        }

    # Mantiene el estado acotado: elimina entradas no vistas hace más de 45 días.
    cutoff = datetime.now(timezone.utc).timestamp() - 45 * 24 * 3600
    for key, value in list(seen.items()):
        try:
            last_seen = datetime.fromisoformat(value["last_seen_utc"]).timestamp()
            if last_seen < cutoff:
                del seen[key]
        except Exception:
            pass

    try:
        fingerprint, page_text = official_page_fingerprint()
        previous = state.get("last_page_fingerprint")
        state["last_page_fingerprint"] = fingerprint
        if previous and previous != fingerprint:
            logging.info("Cambió la página informativa oficial del MSP.")
            # No alerta por cualquier cambio editorial: la agenda real manda.
    except Exception:
        logging.exception("No se pudo revisar la página informativa oficial.")

    save_state(state)

    if new_slots:
    logging.info("Se detectaron %d cupos nuevos.", len(new_slots))

    # Primero manda un resumen general.
    send_telegram(format_summary(new_slots))

    # Después manda un mensaje separado por cada región con cupos.
    for region in ("Sur", "Este", "Oeste", "Norte"):
        region_slots = [
            slot for slot in new_slots
            if slot.region == region
        ]

        if region_slots:
            send_telegram(format_region_alert(region, region_slots))
else:
    logging.info("No se detectaron cupos nuevos.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
