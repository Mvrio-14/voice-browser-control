"""NAVIGATEUR : piloter TON vrai Google Chrome (et pas le Chromium intégré à Playwright).

Chrome peut être piloté de l'extérieur via son protocole de débogage (CDP), si on le lance avec
--remote-debugging-port. Playwright s'y connecte alors (connect_over_cdp) comme à n'importe quel navigateur.

Contrainte de sécurité de Chrome (depuis la version 136) : ce mode est REFUSÉ sur ton profil habituel
(des logiciels malveillants s'en servaient pour voler les cookies). On utilise donc un profil dédié,
« voice_click » : tu t'y connectes une fois à tes sites (Google, YouTube...), et c'est conservé.
Le port n'écoute que sur ta machine (127.0.0.1), et on vérifie que c'est bien Chrome qui répond avant de s'y brancher.
"""
import asyncio
import contextlib
import json
import os
import subprocess
import urllib.request
from pathlib import Path

# Pas 9222 (le port « classique ») : d'autres applis l'utilisent (ex : Lenovo Vantage, faite avec Edge WebView).
PORT = 9339
PROFILE = Path(os.environ["LOCALAPPDATA"]) / "voice_click" / "chrome-profile"
CHROME_PATHS = [
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ["LOCALAPPDATA"]) / "Google/Chrome/Application/chrome.exe",
]


def who_listens(port):
    """Qui répond sur ce port de débogage ? Ex : "Chrome/155.0…", "Edg/154.0…", ou None si personne."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as response:
            return json.load(response).get("Browser", "?")
    except OSError:
        return None


async def open_chrome(playwright, start_url, port=PORT, profile=PROFILE):
    """Se branche sur le Chrome « voice_click » s'il est déjà ouvert, sinon le lance. Renvoie le navigateur."""
    listener = who_listens(port)
    if listener and not listener.startswith("Chrome/"):
        # On ne se branche jamais sur n'importe quoi : une autre appli pourrait occuper ce port.
        raise RuntimeError(f"Le port {port} est déjà utilisé par une autre application ({listener}). "
                           f"Change PORT dans browser.py.")
    if listener:  # déjà ouvert (ex : on relance le programme)
        return await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")

    chrome = next((path for path in CHROME_PATHS if path.exists()), None)
    if chrome is None:
        raise RuntimeError("Google Chrome est introuvable. Installe-le, ou lance avec --playwright.")
    subprocess.Popen([str(chrome), f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
                      "--start-maximized", "--no-first-run", start_url])
    for _ in range(60):  # Chrome met une ou deux secondes à démarrer
        await asyncio.sleep(0.25)
        with contextlib.suppress(Exception):
            return await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    raise RuntimeError("Chrome ne répond pas sur le port de débogage.")


async def tab_list(page):
    """Les onglets ouverts, dans l'ordre : [{"n": 1, "title": "YouTube", "site": "youtube.com", "active": True}, ...]"""
    tabs = []
    for n, tab in enumerate(page.context.pages, start=1):
        with contextlib.suppress(Exception):
            site = tab.url.split("/")[2] if "://" in tab.url else tab.url
            tabs.append({"n": n, "title": (await tab.title())[:80], "site": site.removeprefix("www."),
                         "active": tab is page})
    return tabs


async def follow_real_focus(context):
    """Playwright fait croire à TOUS les onglets qu'ils sont au premier plan (une « émulation de focus », pour
    que les scripts des pages tournent normalement). Impossible alors de savoir quel onglet tu regardes.
    On coupe cette émulation sur chaque onglet, existant ou futur : document.hasFocus() redit la vérité."""
    async def real_focus(page):
        with contextlib.suppress(Exception):
            session = await context.new_cdp_session(page)
            await session.send("Emulation.setFocusEmulationEnabled", {"enabled": False})

    for page in context.pages:
        await real_focus(page)
    context.on("page", lambda page: asyncio.ensure_future(real_focus(page)))


shown_tab = None  # le dernier onglet affiché connu


def show_tab(page):
    """À appeler quand c'est NOUS qui changeons d'onglet (act.py) : on sait alors lequel est affiché."""
    global shown_tab
    shown_tab = page


async def active_page(context, fallback):
    """L'onglet que l'utilisateur regarde. Deux sources :
      - celui qui a le focus (document.hasFocus) : ça détecte aussi un changement d'onglet à la souris ;
      - sinon, le dernier onglet affiché connu. (Aucun onglet n'a le focus quand la fenêtre active de Windows
        n'est pas Chrome, ex : le terminal ; or on a pu changer d'onglet nous-mêmes entre-temps.)"""
    for page in reversed(context.pages):  # (les plus récents d'abord)
        with contextlib.suppress(Exception):
            if await page.evaluate("document.hasFocus()"):
                show_tab(page)
                return page
    for candidate in (shown_tab, fallback):
        if candidate in context.pages:
            return candidate
    return context.pages[-1]
