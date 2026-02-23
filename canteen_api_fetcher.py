# post_meal.py
# Standard library only (no pip install needed)

import json
import os
import sys
import unicodedata
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


API_BASE = os.getenv("CANTINAS_API_BASE", "https://api.cantinas.pt/")
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
TZ_NAME = os.getenv("TZ_NAME", "Europe/Lisbon")
WEBHOOK_USERNAME = os.getenv("WEBHOOK_USERNAME", "Cantina")
TARGET_DATE = os.getenv("TARGET_DATE", "2025-02-24") 
DISCORD_SAFE_LIMIT = 1900
ALLOWED_REFEITORIO_ORDER = ("Santiago", "Crasto")
ALLOWED_REFEITORIO_LOOKUP = {
    name.casefold(): name for name in ALLOWED_REFEITORIO_ORDER
}


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


def clean_text(value):
    return " ".join(str(value or "").strip().split())


def normalize_ascii(value):
    text = clean_text(value)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.casefold()


def canonical_refeitorio_name(value):
    return ALLOWED_REFEITORIO_LOOKUP.get(normalize_ascii(value))


def get_target_date_str() -> str:
    if TARGET_DATE:
        return TARGET_DATE
    tz = ZoneInfo(TZ_NAME)
    return datetime.now(tz).date().isoformat()


def display_date(date_str: str) -> str:
    try:
        return datetime.fromisoformat(date_str).strftime("%d/%m/%Y")
    except ValueError:
        return date_str


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


def component_items(componentes):
    """Return normalized component items preserving display names."""
    items = []

    for comp in componentes or []:
        if not isinstance(comp, dict):
            continue

        nome = clean_text(comp.get("Nome"))
        tipo = clean_text(comp.get("TipoString")) or "Item"
        if not nome:
            continue

        items.append(
            {
                "tipo": tipo,
                "tipo_key": normalize_ascii(tipo),
                "nome": nome,
            }
        )

    return items


def split_soup_components(componentes):
    soups = []
    others = []

    for item in component_items(componentes):
        pair = (item["tipo"], item["nome"])
        if item["tipo_key"] == "sopa":
            soups.append(pair)
        else:
            others.append(pair)

    return soups, others


def format_component_pair(pair):
    tipo, nome = pair
    return f"**{tipo}:** {nome}"


def period_sort_key(periodo: str):
    normalized = normalize_ascii(periodo)
    if normalized == "almoco":
        return (0, normalized)
    if normalized == "jantar":
        return (1, normalized)
    return (99, normalized)


def format_menu_message(payload, target_date: str) -> str:
    """Format API payload into a cleaner Discord message."""
    header = f"**Menu do dia - {display_date(target_date)}**"
    subtitle = "_Refeitorios: Santiago, Crasto_"

    if not isinstance(payload, list):
        preview = json.dumps(payload, ensure_ascii=False, indent=2)
        if len(preview) > 1500:
            preview = preview[:1500] + "\n... (truncated)"
        return (
            f"{header}\n{subtitle}\n"
            "Formato inesperado da API:\n"
            f"```json\n{preview}\n```"
        )

    grouped = {}

    for item in payload:
        if not isinstance(item, dict):
            continue

        periodo = clean_text(item.get("Periodo")) or "Sem periodo"

        raw_refeitorios = item.get("Refeitorios") or []
        if not isinstance(raw_refeitorios, list):
            raw_refeitorios = [raw_refeitorios]

        allowed_refeitorios = []
        for raw_refeitorio in raw_refeitorios:
            canonical = canonical_refeitorio_name(raw_refeitorio)
            if canonical and canonical not in allowed_refeitorios:
                allowed_refeitorios.append(canonical)

        if not allowed_refeitorios:
            continue

        nome_menu = clean_text(item.get("Nome")) or "Menu"
        componentes = item.get("Componentes") or []

        for refeitorio in allowed_refeitorios:
            grouped.setdefault(periodo, {}).setdefault(refeitorio, []).append(
                {
                    "nome_menu": nome_menu,
                    "componentes": componentes,
                }
            )

    if not grouped:
        return f"{header}\n{subtitle}\nSem resultados para Santiago/Crasto."

    periods = sorted(grouped.keys(), key=period_sort_key)

    lines = [header, subtitle, ""]

    for periodo in periods:
        lines.append(f"**{periodo}**")

        period_had_entries = False
        for refeitorio in ALLOWED_REFEITORIO_ORDER:
            entries = grouped[periodo].get(refeitorio)
            if not entries:
                continue

            period_had_entries = True
            lines.append(f"`{refeitorio}`")

            prepared_entries = []
            soup_signatures = []

            for entry in entries:
                soups, others = split_soup_components(entry["componentes"])
                prepared_entries.append(
                    {
                        "nome_menu": entry["nome_menu"],
                        "soups": soups,
                        "others": others,
                    }
                )
                soup_signatures.append(tuple(soups))

            shared_soups = []
            if (
                soup_signatures
                and soup_signatures[0]
                and all(sig == soup_signatures[0] for sig in soup_signatures[1:])
            ):
                shared_soups = list(soup_signatures[0])

            for soup_pair in shared_soups:
                lines.append(f"- {format_component_pair(soup_pair)}")

            for entry in prepared_entries:
                menu_name = entry["nome_menu"]
                soups = [] if shared_soups else entry["soups"]
                others = entry["others"]

                if len(others) == 1 and normalize_ascii(others[0][0]) == "prato":
                    lines.append(f"- **{menu_name}:** {others[0][1]}")
                    if soups:
                        for soup_pair in soups:
                            lines.append(f"  - {format_component_pair(soup_pair)}")
                    continue

                lines.append(f"- **{menu_name}**")

                detail_pairs = others + soups
                if detail_pairs:
                    for pair in detail_pairs:
                        lines.append(f"  - {format_component_pair(pair)}")
                else:
                    lines.append("  - _(sem componentes)_")

            lines.append("")

        if period_had_entries and lines[-1] != "":
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
