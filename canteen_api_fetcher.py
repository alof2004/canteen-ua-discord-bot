# post_meal.py
# Standard library only (no pip install needed)

import json
import os
import sys
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


API_BASE = os.getenv("CANTINAS_API_BASE", "https://api.cantinas.pt/")
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
TZ_NAME = os.getenv("TZ_NAME", "Europe/Lisbon")
WEBHOOK_USERNAME = os.getenv("WEBHOOK_USERNAME", "Cantina")
TARGET_DATE = os.getenv("2025-02-24")  # optional YYYY-MM-DD (for manual testing)
DISCORD_SAFE_LIMIT = 1900


def fail(msg: str, code: int = 1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def http_get_json(url: str):
    req = Request(url, headers={"User-Agent": "cantinas-discord-webhook/1.0"})
    with urlopen(req, timeout=20) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        return json.loads(body)


def http_post_json(url: str, payload: dict):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "cantinas-discord-webhook/1.0",
        },
        method="POST",
    )
    with urlopen(req, timeout=20) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        return resp.status, body


def clean_text(s):
    return " ".join(str(s or "").strip().split())


def get_target_date_str() -> str:
    if TARGET_DATE:
        return TARGET_DATE
    tz = ZoneInfo(TZ_NAME)
    return datetime.now(tz).date().isoformat()


def split_discord_messages(text: str, limit: int = DISCORD_SAFE_LIMIT):
    """Split long content into safe chunks for Discord."""
    if len(text) <= limit:
        return [text]

    chunks = []
    current = ""

    for line in text.splitlines(keepends=True):
        if len(current) + len(line) <= limit:
            current += line
            continue

        if current:
            chunks.append(current.rstrip())
            current = ""

        # If a single line is too long, hard-split it
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]

        current = line

    if current:
        chunks.append(current.rstrip())

    return chunks


def component_lines(componentes):
    """
    Converts:
      {"Nome": "...", "TipoString": "Prato", "Alergenicos": [...]}
    into:
      • Prato: ...
      • Sopa: ...
    """
    lines = []

    for comp in componentes or []:
        if not isinstance(comp, dict):
            continue

        nome = clean_text(comp.get("Nome"))
        tipo = clean_text(comp.get("TipoString")) or "Item"

        if not nome:
            continue

        line = f"• **{tipo}:** {nome}"
        lines.append(line)

    return lines


def format_menu_message(payload, target_date: str) -> str:
    """
    Expected payload is a list like:
    [
      {
        "Periodo": "Almoço",
        "Data": "2026-02-27T00:00:00",
        "Nome": "PRATO CARNE",
        "Refeitorios": ["Santiago"],
        "Componentes": [...]
      },
      ...
    ]
    """
    if not isinstance(payload, list):
        preview = json.dumps(payload, ensure_ascii=False, indent=2)
        if len(preview) > 1500:
            preview = preview[:1500] + "\n... (truncated)"
        return (
            f"🍽️ **Menu do dia — {target_date}**\n"
            "Formato inesperado da API:\n"
            f"```json\n{preview}\n```"
        )

    # Group by Periodo -> Refeitorio
    grouped = {}

    for item in payload:
        if not isinstance(item, dict):
            continue

        periodo = clean_text(item.get("Periodo")) or "Sem período"
        refeitorios = item.get("Refeitorios") or []
        if isinstance(refeitorios, list) and refeitorios:
            refeitorio = clean_text(refeitorios[0]) or "Sem refeitório"
        else:
            refeitorio = "Sem refeitório"

        nome_menu = clean_text(item.get("Nome")) or "Menu"
        componentes = item.get("Componentes") or []

        grouped.setdefault(periodo, {}).setdefault(refeitorio, []).append({
            "nome_menu": nome_menu,
            "componentes": componentes,
        })

    if not grouped:
        return f"🍽️ **Menu do dia — {target_date}**\nSem resultados."

    # Nice period order
    preferred_period_order = ["Almoço", "Jantar"]
    periods = sorted(
        grouped.keys(),
        key=lambda p: (preferred_period_order.index(p) if p in preferred_period_order else 999, p)
    )

    lines = [f"🍽️ **Menu do dia — {target_date}**", ""]

    for periodo in periods:
        lines.append(f"## {periodo}")

        for refeitorio in sorted(grouped[periodo].keys()):
            lines.append(f"**📍 {refeitorio}**")

            for entry in grouped[periodo][refeitorio]:
                lines.append(f"**{entry['nome_menu']}**")
                comp = component_lines(entry["componentes"])
                if comp:
                    lines.extend(comp)
                else:
                    lines.append("• _(sem componentes)_")
                lines.append("")

        lines.append("")

    return "\n".join(lines).rstrip()


def post_to_discord(text: str):
    chunks = split_discord_messages(text, DISCORD_SAFE_LIMIT)

    for i, chunk in enumerate(chunks, start=1):
        payload = {
            "content": chunk,
            "username": WEBHOOK_USERNAME,
            "allowed_mentions": {"parse": []},
        }
        status, body = http_post_json(WEBHOOK_URL, payload)
        print(f"Posted chunk {i}/{len(chunks)} (HTTP {status})")
        if status not in (200, 204):
            print(body, file=sys.stderr)


def main():
    if not WEBHOOK_URL:
        fail("DISCORD_WEBHOOK_URL is missing.")

    target_date = get_target_date_str()
    api_url = f"{API_BASE}?{urlencode({'date': target_date})}"
    print(f"Fetching {api_url}")

    try:
        payload = http_get_json(api_url)
    except HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        fail(f"API HTTP error {e.code} {e.reason}\n{body}")
    except URLError as e:
        fail(f"API URL error: {e}")
    except json.JSONDecodeError as e:
        fail(f"Invalid JSON from API: {e}")
    except Exception as e:
        fail(f"API fetch error: {e}")

    message = format_menu_message(payload, target_date)
    print("Formatted message length:", len(message))

    try:
        post_to_discord(message)
    except HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        fail(f"Webhook HTTP error {e.code} {e.reason}\n{body}")
    except URLError as e:
        fail(f"Webhook URL error: {e}")
    except Exception as e:
        fail(f"Webhook post error: {e}")


if __name__ == "__main__":
    main()