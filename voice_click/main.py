"""voice_click : je dis « clique sur X », il clique sur X.

La boucle :  demande → voir → décider → agir
Une demande peut venir de trois sources (la première qui arrive gagne) :
  - la barre flottante dans la page (panel.py) : on y tape une commande
  - la voix (sauf avec --type) : « hey browser » (ou Ctrl+Maj+Espace) ouvre une SESSION, pendant laquelle
    on enchaîne les commandes sans rien toucher. 5 s sans que TU parles (ou le raccourci) : retour en veille.
    (Ne suivre que la voix qui a dit « hey browser » : speaker.py, désactivé, voir FOLLOW_VOICE.)
  - le terminal (avec --type)

Le navigateur piloté est TON Google Chrome, avec un profil dédié « voice_click » (voir browser.py).

Lancer (depuis le dossier voice_browser) :
    .venv/Scripts/python voice_click/main.py                 # démarre sur YouTube
    .venv/Scripts/python voice_click/main.py https://fr.wikipedia.org
    .venv/Scripts/python voice_click/main.py --type          # sans micro : terminal + barre
    .venv/Scripts/python voice_click/main.py --playwright    # le Chromium intégré à Playwright, au lieu de Chrome
    .venv/Scripts/python voice_click/main.py --no-image      # décider sur le texte seul, sans capture d'écran
    .venv/Scripts/python voice_click/main.py --gradium       # transcription par Gradium au lieu d'AssemblyAI
"""
import asyncio
import contextlib
import re
import sys
import time

from dotenv import load_dotenv

load_dotenv()  # lit GRADIUM_API_KEY et OPENAI_API_KEY dans .env (AVANT d'importer nos modules, qui créent les clients)
sys.stdout.reconfigure(encoding="utf-8")  # la console Windows n'affiche pas les emojis sans ça

from playwright.async_api import async_playwright
from pynput import keyboard

import browser
import listen as ears
import motion
import panel
from act import act
from decide import decide
from listen import AssemblyAISession, Microphone, listen, wait_for_wake
from see import see

# Le raccourci. Pas F8 & co : sur un portable, ce sont souvent des touches système (mode avion...).
HOTKEY = "<ctrl>+<shift>+<space>"
HOTKEY_LABEL = "Ctrl+Maj+Espace"
HINT = f"« Hey browser » ou {HOTKEY_LABEL}"  # ce qu'affiche la barre flottante
# Couper une phrase quand la voix change (listen.py) : DÉSACTIVÉ. Testé avec des voix de synthèse, mais avec
# une vraie voix au café, l'empreinte sur 1,5 s est trop faible : ça coupait tes propres phrases en morceaux.
CUT_ON_VOICE_CHANGE = False
# Ne suivre que la voix qui a dit « hey browser » (speaker.py) : DÉSACTIVÉ. Avec une vraie voix, l'empreinte
# tirée du seul « hey browser » est trop faible : tes commandes courtes (« stop », « plus vite ») étaient rejetées.
FOLLOW_VOICE = False
if FOLLOW_VOICE:
    import speaker  # (chargé seulement s'il sert : le modèle d'empreinte vocale ralentit le démarrage)
RETRY_S = 3  # après une erreur d'écoute (réseau, service de transcription...), on attend 3 s avant de réessayer
SESSION_IDLE_S = 5  # en session : si TU ne dis rien pendant 5 s (après la dernière action), on repasse en veille
# « hey browser » redit pendant une session (Gradium l'écrit parfois « hey brother ») : ce n'est pas une commande.
WAKE_AGAIN = re.compile(r"^(hey|hé|eh|et)\W+(browser|brother|brow\w*)\W*$", re.IGNORECASE)
START_URL = next((a for a in sys.argv[1:] if a.startswith("http")), "https://www.youtube.com")
TYPE_MODE = "--type" in sys.argv
PLAYWRIGHT_MODE = "--playwright" in sys.argv
# Sans capture : le modèle décide sur la liste des éléments seule. Mesuré (eval/image_latency.py) : ~280 ms
# par décision au lieu de ~515 ms, mais les demandes visuelles (« la vidéo avec le chat ») échouent.
WITH_IMAGE = "--no-image" not in sys.argv
if "--gradium" in sys.argv:
    ears.ENGINE = "gradium"

terminal_line = None  # la lecture d'une ligne du terminal (gardée d'un tour à l'autre, voir get_request)
hotkey = asyncio.Event()  # levé quand on appuie sur le raccourci (voir watch_hotkey)
# None = veille ; sinon {"voice": la voix qui a dit « hey browser » (speaker.Voice), ou None,
#                       "stream": la connexion AssemblyAI de la session (AssemblyAISession), ou None}
session = None


def read_line():
    try:
        return input("\n⌨️  Ta commande : ")
    except EOFError:  # (Ctrl+C / terminal fermé pendant la lecture)
        return ""


def watch_hotkey():
    """Surveille le raccourci pendant tout le programme (n'importe où : même si la fenêtre active est le navigateur)."""
    loop = asyncio.get_running_loop()
    # (la fonction est appelée par pynput dans un autre thread : on prévient la boucle asyncio)
    keyboard.GlobalHotKeys({HOTKEY: lambda: loop.call_soon_threadsafe(hotkey.set)}).start()


async def first(*tasks):
    """Attend la première tâche terminée, annule les autres, et la renvoie."""
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    return done.pop()


async def end_session(reason):
    """Retour en veille : on ferme aussi la connexion au service de transcription de la session."""
    global session
    if session and session["stream"]:
        await session["stream"].close()
    session = None
    if reason:
        print(f"💤 {reason}")


async def wake_up(here, mic):
    """Veille : attend « hey browser » ou le raccourci, puis ouvre la session.
    here() : l'onglet que tu regardes maintenant (le panneau s'affiche là)."""
    global session
    print(f"\n💤 Veille : dis « hey browser » (ou {HOTKEY_LABEL}), ou tape dans la barre")
    await panel.show(await here(), "idle")
    wake = asyncio.create_task(wait_for_wake(mic))
    if await first(wake, asyncio.create_task(hotkey.wait())) is wake:
        # L'empreinte de la voix qui vient de parler : seule elle sera écoutée pendant la session.
        voice = await asyncio.to_thread(speaker.Voice, wake.result()) if FOLLOW_VOICE else None
        session = {"voice": voice, "stream": None}
        print("🟢 Hey browser ! Je t'écoute" + (" (et seulement ta voix)" if voice else "") + ". Enchaîne les commandes.")
    else:
        session = {"voice": None, "stream": None}  # (au clavier : pas de voix de référence, on écoute tout le monde)
        print("🟢 Je t'écoute. Enchaîne les commandes.")
    hotkey.clear()
    if ears.ENGINE == "assemblyai":  # une seule connexion pour toute la session (elle reçoit le micro en continu)
        session["stream"] = AssemblyAISession(mic)
        await session["stream"].open()
    await panel.show(await here(), "listening", "À l'écoute", "Dis une commande…")


async def voice(context, page, mic):
    """La prochaine demande dite au micro. En veille, il faut d'abord la réveiller. Pendant un mouvement
    continu (défilement, avance rapide...), on écoute directement, même en veille, et le mouvement
    s'arrête dès que Silero entend une voix : avant même de savoir ce qui est dit."""
    global session

    async def here():
        """L'onglet que tu regardes maintenant : le panneau s'affiche là (tu as pu changer d'onglet entre-temps)."""
        nonlocal page
        page = await browser.active_page(context, page)
        return page

    deadline = time.monotonic() + SESSION_IDLE_S  # (les voix ignorées ne repoussent pas l'échéance : seule la tienne)
    while True:
        if session is None and not motion.running():
            await wake_up(here, mic)
            deadline = time.monotonic() + SESSION_IDLE_S
        if motion.running():  # un défilement en cours garde la session ouverte (pour pouvoir dire « stop »)
            deadline = time.monotonic() + SESSION_IDLE_S
        remaining = deadline - time.monotonic()
        if session is not None and remaining <= 0:
            await end_session(f"{SESSION_IDLE_S} s sans ta voix : je repasse en veille.")
            continue

        async def show_level():  # l'onde du panneau suit le volume du micro
            while True:
                await panel.level(page, mic.level)
                await asyncio.sleep(0.08)

        async def on_voice():
            await motion.stop(page)
            await panel.show(await here(), "listening", "J'écoute", "…")

        async def on_text(text):
            await panel.show(await here(), "listening", "J'écoute", text)

        level = asyncio.create_task(show_level())
        followed = session["voice"] if session else None
        heard = asyncio.create_task(listen(mic, max(remaining, 0.1), on_voice=on_voice,
                                           voice=followed if CUT_ON_VOICE_CHANGE else None, on_text=on_text,
                                           stream=session["stream"] if session else None))
        try:
            pressed = await first(heard, asyncio.create_task(hotkey.wait())) is not heard
        finally:
            level.cancel()
        if pressed:  # le raccourci pendant la session : on la ferme
            hotkey.clear()
            await end_session("Session fermée.")
            continue

        text, audio = heard.result()
        if text is None:  # personne n'a parlé avant l'échéance (vérifiée en haut de la boucle)
            continue
        if not text.strip():  # une voix, mais aucun mot compris (toux, bruit)
            continue
        if followed:
            score, resemblance, db = await asyncio.to_thread(followed.score, audio)
            detail = f"score {score:.2f} : timbre {resemblance:.2f}, volume {db:+.0f} dB par rapport au réveil"
            if score < speaker.SAME_VOICE:
                print(f"🙉 « {text} » ignoré : ce n'est pas la voix du réveil ({detail})")
                await panel.show(await here(), "listening", "Autre voix ignorée", text)
                continue
            print(f"   (ta voix : {detail})")
        if WAKE_AGAIN.match(text.strip()):  # la session était déjà ouverte : on le confirme, sans rien faire d'autre
            print("👋 Oui, je t'écoute.")
            await panel.show(await here(), "listening", "À l'écoute", "Oui, je t'écoute…")
            deadline = time.monotonic() + SESSION_IDLE_S
            continue
        return text


async def get_request(context, page, mic):
    """Attend la prochaine demande, quelle que soit sa source : la première qui arrive gagne."""
    global terminal_line
    sources = [asyncio.create_task(panel.next_command())]
    if TYPE_MODE:
        # La lecture du terminal ne peut pas être annulée proprement : on la garde d'un tour à l'autre.
        if terminal_line is None or terminal_line.done():
            terminal_line = asyncio.create_task(asyncio.to_thread(read_line))
        sources.append(terminal_line)
    else:
        sources.append(asyncio.create_task(voice(context, page, mic)))

    done, _ = await asyncio.wait(sources, return_when=asyncio.FIRST_COMPLETED)
    for source in sources:
        if source is not terminal_line and not source.done():
            source.cancel()  # (ex : on a tapé dans la barre pendant que le micro attendait « hey browser »)
    return done.pop().result()


async def main():
    async with async_playwright() as p:
        context = await open_browser(p)
        page = await browser.active_page(context, None)
        history = []  # les dernières demandes et ce qui a été fait : la mémoire de la conversation
        with contextlib.nullcontext() if TYPE_MODE else Microphone() as mic:  # (le micro reste ouvert)
            if not TYPE_MODE:
                watch_hotkey()
                print(f"🎙️  Transcription : {'AssemblyAI' if ears.ENGINE == 'assemblyai' else 'Gradium'}")
            print(f"🖼️  Capture d'écran envoyée au modèle : {'oui' if WITH_IMAGE else 'non (--no-image)'}")

            while True:
                # Une erreur (page qui change pendant l'action, réseau, micro...) ne doit jamais arrêter
                # le programme : on l'affiche, et on attend la commande suivante.
                page = await browser.active_page(context, page)  # (l'onglet que tu regardes)
                try:
                    request = await get_request(context, page, mic)  # 1. OREILLES (ou la barre, ou le terminal)
                except Exception as error:
                    # On ferme la session (sa connexion est sans doute cassée), et on attend un peu au lieu de
                    # réessayer aussitôt : une erreur qui se répète ne doit pas tourner en boucle.
                    print(f"💥 Écoute impossible : {error!r}")
                    await end_session(None)
                    await panel.show(page, "error", "Écoute impossible", f"{error} — nouvel essai dans {RETRY_S} s")
                    await asyncio.sleep(RETRY_S)
                    continue
                if not request.strip():
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
        await panel.install(context, HINT)
        await browser.follow_real_focus(context)  # (pour savoir quel onglet tu regardes)
        await context.pages[0].goto(START_URL)
        return context

    chrome = await browser.open_chrome(p, START_URL)
    context = chrome.contexts[0]
    await panel.install(context, HINT)  # (pour les pages qui se chargeront ensuite)
    await browser.follow_real_focus(context)  # (pour savoir quel onglet tu regardes)
    for page in context.pages:  # ... et les onglets déjà ouverts
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("domcontentloaded")
            await page.evaluate(panel.panel_js(HINT))
    return context


async def handle(page, request, history):
    """Une commande : voir → décider → agir. Renvoie la phrase qui décrit ce qui a été fait."""
    t0 = time.perf_counter()
    elements, screenshot, state = await see(page, WITH_IMAGE)  # 2. YEUX
    tabs = await browser.tab_list(page)
    t1 = time.perf_counter()
    d = decide(request, elements, screenshot, state, history, tabs)  # 3. CERVEAU
    t2 = time.perf_counter()
    print(f"👀 {len(elements)} éléments ({(t1 - t0) * 1000:.0f} ms)"
          f"  🧠 {d['action']} (confiance {d['confidence']:.2f}) cible={d['target']} ({d['target_confidence']:.2f})"
          f" texte={d['text']!r} ampleur={d['amount']} durée={d['duration']} onglet={d['tab']}  ({(t2 - t1) * 1000:.0f} ms)")
    return await act(page, d, elements)  # 4. MAINS


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # Ctrl+C
        print("\n👋 Au revoir.")
