"""voice_click : je dis « clique sur X », il clique sur X.

La boucle :  demande → voir → décider → agir
Une demande peut venir de trois sources (la première qui arrive gagne) :
  - la barre flottante dans la page (panel.py) : on y tape une commande
  - la voix : Ctrl+Maj+Espace, puis on parle (sauf avec --type)
  - le terminal (avec --type)

Le navigateur piloté est TON Google Chrome, avec un profil dédié « voice_click » (voir browser.py).

Lancer (depuis le dossier voice_browser) :
    .venv/Scripts/python voice_click/main.py                 # démarre sur YouTube
    .venv/Scripts/python voice_click/main.py https://fr.wikipedia.org
    .venv/Scripts/python voice_click/main.py --type          # sans micro : terminal + barre
    .venv/Scripts/python voice_click/main.py --playwright    # le Chromium intégré à Playwright, au lieu de Chrome
"""
import asyncio
import contextlib
import sys
import time

from dotenv import load_dotenv

load_dotenv()  # lit GRADIUM_API_KEY et OPENAI_API_KEY dans .env (AVANT d'importer nos modules, qui créent les clients)
sys.stdout.reconfigure(encoding="utf-8")  # la console Windows n'affiche pas les emojis sans ça

from playwright.async_api import async_playwright
from pynput import keyboard

import browser
import motion
import panel
from act import act
from decide import decide
from listen import listen
from see import see

# Le raccourci pour parler. Pas F8 & co : sur un portable, ce sont souvent des touches système (mode avion...).
HOTKEY = "<ctrl>+<shift>+<space>"
HOTKEY_LABEL = "Ctrl+Maj+Espace"
START_URL = next((a for a in sys.argv[1:] if a.startswith("http")), "https://www.youtube.com")
TYPE_MODE = "--type" in sys.argv
PLAYWRIGHT_MODE = "--playwright" in sys.argv

terminal_line = None  # la lecture d'une ligne du terminal (gardée d'un tour à l'autre, voir get_request)


def read_line():
    try:
        return input("\n⌨️  Ta commande : ")
    except EOFError:  # (Ctrl+C / terminal fermé pendant la lecture)
        return ""


async def wait_for_hotkey():
    """Attend le raccourci pour parler (n'importe où : même si la fenêtre active est le navigateur)."""
    loop = asyncio.get_running_loop()
    pressed = asyncio.Event()
    # (la fonction est appelée par pynput dans un autre thread : on prévient la boucle asyncio)
    with keyboard.GlobalHotKeys({HOTKEY: lambda: loop.call_soon_threadsafe(pressed.set)}):
        await pressed.wait()


async def voice(page):
    """Une demande dite au micro, après le raccourci. Pendant un mouvement continu (défilement, avance rapide...),
    on écoute directement (sans raccourci), et le mouvement s'arrête dès que Silero entend une voix :
    avant même de savoir ce qui est dit."""
    if not motion.running():
        await wait_for_hotkey()
    await panel.show(page, "listening", "J'écoute", "…")
    return await listen(
        on_voice=lambda: motion.stop(page),
        on_text=lambda text: panel.show(page, "listening", "J'écoute", text),
        on_level=lambda v: panel.level(page, v),
    )


async def get_request(page):
    """Attend la prochaine demande, quelle que soit sa source : la première qui arrive gagne."""
    global terminal_line
    sources = [asyncio.create_task(panel.next_command())]
    if TYPE_MODE:
        # La lecture du terminal ne peut pas être annulée proprement : on la garde d'un tour à l'autre.
        if terminal_line is None or terminal_line.done():
            terminal_line = asyncio.create_task(asyncio.to_thread(read_line))
        sources.append(terminal_line)
    else:
        print(f"\n⏸️  {HOTKEY_LABEL} pour parler, ou tape dans la barre")
        sources.append(asyncio.create_task(voice(page)))

    done, _ = await asyncio.wait(sources, return_when=asyncio.FIRST_COMPLETED)
    for source in sources:
        if source is not terminal_line and not source.done():
            source.cancel()  # (ex : on a tapé dans la barre pendant que le micro attendait le raccourci)
    return done.pop().result()


async def main():
    async with async_playwright() as p:
        context = await open_browser(p)
        page = await browser.active_page(context, None)
        history = []  # les dernières demandes et ce qui a été fait : la mémoire de la conversation

        while True:
            # Une erreur (page qui change pendant l'action, réseau, micro...) ne doit jamais arrêter
            # le programme : on l'affiche, et on attend la commande suivante.
            page = await browser.active_page(context, page)  # (l'onglet que tu regardes)
            try:
                request = await get_request(page)  # 1. OREILLES (ou la barre, ou le terminal)
            except Exception as error:
                print(f"💥 Écoute impossible : {error!r}")
                await panel.show(page, "error", "Erreur", "Écoute impossible, réessaie")
                continue
            if not request.strip():
                await panel.show(page, "idle")
                continue
            await motion.stop(page)  # une nouvelle commande arrête toujours le mouvement en cours
            page = await browser.active_page(context, page)  # (tu as pu changer d'onglet pendant ce temps)
            print(f"🎤 « {request} »")
            await panel.show(page, "thinking", "Tu as dit", request)

            try:
                done = await handle(page, request, history)
            except Exception as error:
                done = f"💥 erreur : {error!r}"
            print(done)
            status = "moving" if motion.running() else "done" if done[0] not in "🤷💥" else "error"
            await panel.show(page, status, request, done)
            history = (history + [f"« {request} » → {done}"])[-5:]  # on garde les 5 dernières


async def open_browser(p):
    """Ouvre le navigateur et y installe la barre flottante. Renvoie le « contexte » (l'ensemble des onglets)."""
    if PLAYWRIGHT_MODE:
        # Le Chromium de Playwright, avec un profil persistant (cookies gardés entre deux lancements).
        # no_viewport : la page prend la taille de la fenêtre (et la suit si on la redimensionne).
        context = await p.chromium.launch_persistent_context(
            ".browser-profile", headless=False, no_viewport=True, args=["--start-maximized"])
        await panel.install(context, HOTKEY_LABEL)
        await browser.follow_real_focus(context)  # (pour savoir quel onglet tu regardes)
        await context.pages[0].goto(START_URL)
        return context

    chrome = await browser.open_chrome(p, START_URL)
    context = chrome.contexts[0]
    await panel.install(context, HOTKEY_LABEL)  # (pour les pages qui se chargeront ensuite)
    await browser.follow_real_focus(context)  # (pour savoir quel onglet tu regardes)
    for page in context.pages:  # ... et les onglets déjà ouverts
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("domcontentloaded")
            await page.evaluate(panel.panel_js(HOTKEY_LABEL))
    return context


async def handle(page, request, history):
    """Une commande : voir → décider → agir. Renvoie la phrase qui décrit ce qui a été fait."""
    t0 = time.perf_counter()
    elements, screenshot, state = await see(page)  # 2. YEUX
    tabs = await browser.tab_list(page)
    t1 = time.perf_counter()
    d = decide(request, elements, screenshot, state, history, tabs)  # 3. CERVEAU
    t2 = time.perf_counter()
    print(f"👀 {len(elements)} éléments ({(t1 - t0) * 1000:.0f} ms)"
          f"  🧠 {d['action']} (confiance {d['confidence']:.2f}) cible={d['target']} ({d['target_confidence']:.2f})"
          f" texte={d['text']!r} ampleur={d['amount']} durée={d['duration']} onglet={d['tab']}  ({(t2 - t1) * 1000:.0f} ms)")
    return await act(page, d, elements)  # 4. MAINS


try:
    asyncio.run(main())
except KeyboardInterrupt:  # Ctrl+C
    print("\n👋 Au revoir.")
