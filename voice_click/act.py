"""MAINS : exécuter ce que le modèle a choisi. C'est le code qui agit, jamais le modèle.

Les actions ponctuelles sont ici. Les mouvements continus (« jusqu'à stop », « jusqu'à une condition »)
sont dans motion.py.
"""
import re
from urllib.parse import quote_plus

import browser
import edit
import motion
import see as eyes  # le zoom et la vidéo principale sont gérés dans see.py

MIN_CONFIDENCE = 0.5  # en dessous, on préfère ne rien faire plutôt que d'agir au hasard

# Le moteur des recherches web : la page de résultats Google, comme une recherche normale.
# (Si Google affiche un captcha au navigateur automatisé, DuckDuckGo : "https://duckduckgo.com/?q=")
WEB_SEARCH_URL = "https://www.google.com/search?q="

# Selon l'ampleur demandée : de combien de paliers zoomer, de combien de pixels défiler, de combien
# de secondes avancer dans la vidéo. « au maximum » = jusqu'au bout.
ZOOM_STEPS = {"un peu": 1, "normal": 2, "beaucoup": 4, "au maximum": len(eyes.ZOOM_LEVELS)}
SCROLL_PX = {"un peu": 200, "normal": 600, "beaucoup": 1500, "au maximum": None}  # None = toute la page
SCROLL_STEPS = 15  # le défilement est découpé en 15 petits crans de molette, pour un mouvement fluide
SEEK_S = {"un peu": 5, "normal": 10, "beaucoup": 60, "au maximum": 10**6}  # 10**6 = jusqu'au bout
VOLUME_STEP = {"un peu": 0.1, "normal": 0.2, "beaucoup": 0.4, "au maximum": 1}  # volume de 0 à 1

# Les commandes vidéo passent par l'API vidéo du navigateur (l'élément <video>) : ça marche sur
# presque tous les sites, sans dépendre de leurs raccourcis clavier. Renvoie [temps, volume] après l'action.
VIDEO_JS = f"""([op, value]) => {{
  const v = {eyes.MAIN_VIDEO};
  if (!v) return null;
  if (op === 'play') v.play();
  if (op === 'pause') v.pause();
  if (op === 'mute') v.muted = true;
  if (op === 'unmute') v.muted = false;
  if (op === 'seek') v.currentTime = Math.max(0, Math.min(v.duration, v.currentTime + value));
  if (op === 'goto') v.currentTime = Math.min(v.duration, value);
  if (op === 'goto_fraction') v.currentTime = v.duration * value;  // « va à la moitié », « à 80 % »
  if (op === 'volume') {{
    v.volume = Math.max(0, Math.min(1, v.volume + value));
    if (value > 0) v.muted = false;  // monter le son d'une vidéo coupée : on la rallume
  }}
  return [v.currentTime, v.volume];
}}"""

# La page a pu bouger depuis la capture (images qui se chargent, pubs...). On remesure donc la
# position de l'élément juste avant de cliquer, en le ramenant à l'écran s'il en est sorti.
CENTER_JS = """n => {
  const el = window.__voiceClickElements[n - 1];   // le même élément que celui numéroté par see.py
  el.scrollIntoView({ block: 'nearest' });
  const r = window.__voiceClickBox(el);
  return [r.left + r.width / 2, r.top + r.height / 2];
}"""


async def act(page, d, elements):
    """Exécute la décision d (voir decide.py) :
    {"action", "confidence", "target", "target_confidence", "text", "amount", "duration", "request"}.
    Renvoie une phrase qui décrit ce qui a été fait : main.py l'affiche et la garde en mémoire,
    pour que le modèle sache ce qui vient de se passer (« encore », « plus », « l'autre »...)."""
    action, target, text, amount, duration = d["action"], d["target"], d["text"], d["amount"], d["duration"]

    if action == "none" or d["confidence"] < MIN_CONFIDENCE:
        return "🤷 rien fait (demande pas comprise)"

    if action == "stop":
        return "⏹️  arrêté"  # (main.py a déjà arrêté le mouvement en cours dès que la commande est arrivée)

    # Un mouvement en continu : jusqu'à « stop », ou jusqu'à une condition → motion.py
    if action in motion.MOTIONS and duration != "once":
        # La condition extraite de la phrase. Si elle est vide, motion.py jugera sur la phrase complète.
        condition = text if duration == "until_condition" else None
        if action.startswith("video") and await page.evaluate(VIDEO_JS, ["seek", 0]) is None:
            return "🤷 rien fait (pas de vidéo sur la page)"
        await motion.start(page, action, amount, condition, d["request"], until_stop=duration == "until_stop")
        until = "jusqu'à « stop »" if duration == "until_stop" else f"jusqu'à « {condition or d['request']} »"
        return f"🔁 {action} en continu {until} (dis « stop » pour arrêter)"

    if action == "type_text":
        # Le champ visé, s'il est à l'écran ; sinon celui où se trouve déjà le curseur.
        if target and d["target_confidence"] >= MIN_CONFIDENCE:
            await click(page, target)
        return await edit.edit(page, d["request"], d.get("history", ()))

    if action == "undo_text":
        return await edit.undo(page)

    if action in ("search", "web_search"):
        if not text:
            return "🤷 rien fait (je n'ai pas compris quoi écrire)"
        if action == "web_search" or (action == "search" and not target):
            # La barre d'adresse fait partie de l'interface de Chrome, pas de la page : on ne peut pas y
            # écrire. On ouvre donc directement la page de résultats du moteur de recherche.
            await page.goto(WEB_SEARCH_URL + quote_plus(text))
            if "/sorry/" in page.url:  # la page « trafic exceptionnel » de Google : c'est à l'humain de la passer
                return "🤷 Google demande une vérification (captcha) : fais-la à la main, puis répète la commande"
            reason = "" if action == "web_search" else " (pas de barre de recherche sur cette page)"
            return f"🌐 recherche web de « {text} »{reason}"
        if not target or d["target_confidence"] < MIN_CONFIDENCE:
            return "🤷 rien fait (je ne vois pas dans quel champ écrire)"
        await write(page, target, text)
        await page.keyboard.press("Enter")
        return f"🔎 recherche de « {text} » sur le site"

    if action == "press_enter":
        await page.keyboard.press("Enter")
        return "⏎ appui sur Entrée"

    if action == "press_space":
        await page.keyboard.press("Space")
        return "␣ appui sur Espace"

    if action == "open_url":
        url = to_url(text or "")
        if not url:
            return "🤷 rien fait (il faut l'adresse complète, par exemple « youtube.com »)"
        await page.goto(url)
        return f"🌐 ouverture de {url}"

    if action in ("scroll_down", "scroll_up"):
        # La molette fait défiler ce qui est SOUS la souris (ex : une fenêtre de cookies) :
        # on la place donc au centre de l'écran avant de tourner.
        x, y = await page.evaluate("[innerWidth / 2, innerHeight / 2]")  # le centre de la fenêtre
        await page.mouse.move(x, y)
        px = SCROLL_PX[amount] or await page.evaluate("document.documentElement.scrollHeight")
        step = px / SCROLL_STEPS * (1 if action == "scroll_down" else -1)
        for _ in range(SCROLL_STEPS):  # des petits crans plutôt qu'un saut d'un coup
            await page.mouse.wheel(0, step)
            await page.wait_for_timeout(16)  # ~60 images par seconde : l'œil voit un mouvement fluide
        return ("⬇️  descendu" if action == "scroll_down" else "⬆️  remonté") + f" ({amount})"

    if action in ("zoom_in", "zoom_out", "zoom_reset"):
        if action == "zoom_reset":
            eyes.zoom = 1.0
            await eyes.apply_zoom(page)
        else:
            await eyes.change_zoom(page, ZOOM_STEPS[amount] * (1 if action == "zoom_in" else -1))
        verb = {"zoom_in": "zoomé", "zoom_out": "dézoomé", "zoom_reset": "zoom remis"}[action]
        return f"🔍 {verb} à {eyes.zoom:.0%} ({amount})"

    if action == "back":
        await page.go_back()
        return "⬅️  retour à la page précédente"

    if action in ("tab_new", "tab_switch", "tab_close"):
        return await tabs(page, action, d["tab"], text)

    if action == "reload":
        await page.reload()
        return "🔄 page rechargée"

    if action.startswith("video_"):
        return await video(page, action, text, amount)

    if action == "click":
        if not target or d["target_confidence"] < MIN_CONFIDENCE:
            return "🤷 rien fait (je ne sais pas sur quoi cliquer)"
        e = elements[int(target) - 1]
        await click(page, target)
        return f"🖱️  clic sur le n°{e['n']} {e['role']} « {e['name']} »"

    return f"🤷 rien fait (action inconnue : {action})"


async def click(page, n):
    x, y = await page.evaluate(CENTER_JS, int(n))
    await page.mouse.click(x, y)  # un vrai clic de souris, au centre de l'élément


async def write(page, target, text):
    """Pour une recherche : clique sur la barre, efface son contenu, puis tape le texte."""
    await click(page, target)
    await page.keyboard.press("Control+A")  # sélectionne l'ancien texte pour le remplacer
    await page.keyboard.type(text)


def to_url(text):
    """« youtube point com » → https://youtube.com. Pas de point = pas une adresse → None (on ne devine pas)."""
    t = text.lower().replace(" point ", ".").replace(" dot ", ".").replace(" ", "")
    return "https://" + t if "." in t else None


def to_seconds(text):
    """Un moment ou une durée dit à l'oral → secondes. Sans unité, on compte en minutes.
    « 12 » → 720 · « 12:30 » ou « 12 minutes 30 » → 750 · « 30 secondes » → 30 · aucun nombre → None"""
    numbers = [int(n) for n in re.findall(r"\d+", text or "")]
    if not numbers:
        return None
    if len(numbers) >= 2:
        return numbers[0] * 60 + numbers[1]
    return numbers[0] if "seconde" in text else numbers[0] * 60


def to_fraction(text):
    """Une proportion dite à l'oral → entre 0 et 1. « 80 % » → 0.8 · « la moitié » → 0.5 ·
    « trois quarts » / « 3 quarts » → 0.75 · « un quart » → 0.25 · « deux tiers » → 0.67 · sinon None"""
    t = (text or "").lower()
    if m := re.search(r"(\d+)\s*(%|pour ?cent)", t):
        return int(m.group(1)) / 100
    for words, fraction in [(("trois quarts", "3 quarts", "3/4"), 0.75), (("deux tiers", "2 tiers", "2/3"), 0.67),
                            (("moitié", "milieu"), 0.5), (("tiers",), 0.33), (("quart",), 0.25)]:
        if any(w in t for w in words):
            return fraction
    return None


async def video(page, action, text, amount):
    """Les commandes vidéo ponctuelles. Le temps renvoyé par la page décrit le résultat (pour la mémoire)."""
    if action in ("video_forward", "video_back"):
        seconds = to_seconds(text) or SEEK_S[amount]  # « avance de 30 secondes », sinon selon l'ampleur
        op, value = "seek", seconds if action == "video_forward" else -seconds
    elif action == "video_goto":
        if (fraction := to_fraction(text)) is not None:  # « va à la moitié », « à 80 % »
            op, value = "goto_fraction", fraction
        elif (seconds := to_seconds(text)) is not None:  # « va à la minute 12 », « à 3:30 »
            op, value = "goto", seconds
        else:
            return "🤷 rien fait (je n'ai pas compris à quel moment aller)"
    elif action in ("video_volume_up", "video_volume_down"):
        op, value = "volume", VOLUME_STEP[amount] * (1 if action == "video_volume_up" else -1)
    else:  # video_play, video_pause, video_mute, video_unmute
        op, value = action.removeprefix("video_"), 0

    result = await page.evaluate(VIDEO_JS, [op, value])
    if result is None:
        return "🤷 rien fait (pas de vidéo sur la page)"
    if op in ("seek", "goto", "goto_fraction"):
        await eyes.wait_video_ready(page)  # un saut fait recharger la vidéo : on attend qu'elle soit prête
    time, volume = result
    labels = {"play": "▶️  lecture", "pause": "⏸️  pause", "mute": "🔇 son coupé", "unmute": "🔊 son remis",
              "seek": f"{'⏩ avancé' if value >= 0 else '⏪ reculé'} de {abs(value)} s",
              "goto": "🎯 saut", "goto_fraction": f"🎯 saut à {value:.0%}",
              "volume": f"{'🔊 son monté' if value > 0 else '🔉 son baissé'} à {volume:.0%}"}
    return f"{labels[op]} (vidéo à {eyes.mmss(time)})"


async def tabs(page, action, tab, text):
    """Les onglets. main.py suit ensuite tout seul l'onglet affiché (browser.active_page)."""
    context = page.context
    if action == "tab_new":
        new = await context.new_page()
        url = to_url(text or "")  # « ouvre youtube.com dans un nouvel onglet »
        if url:
            await new.goto(url)
        await new.bring_to_front()
        browser.show_tab(new)
        return "🗂️  nouvel onglet" + (f" sur {url}" if url else "")

    pages = context.pages
    i = pages.index(page) if page in pages else 0
    if tab == "next":
        target = pages[(i + 1) % len(pages)]
    elif tab == "previous":
        target = pages[(i - 1) % len(pages)]
    elif tab and tab.startswith("tab") and 1 <= int(tab[3:]) <= len(pages):
        target = pages[int(tab[3:]) - 1]
    else:
        target = page  # (pas d'onglet précis : celui affiché)
    title = await target.title() or "nouvel onglet"

    if action == "tab_switch":
        if target is page:
            return f"🤷 rien fait (on est déjà sur l'onglet « {title} »)"
        await target.bring_to_front()
        browser.show_tab(target)
        return f"🗂️  onglet « {title} »"

    # tab_close. Fermer le dernier onglet fermerait Chrome : on en ouvre d'abord un vide.
    if len(pages) == 1:
        neighbour = await context.new_page()
    else:  # l'onglet voisin de celui qu'on ferme (s'il était affiché, c'est lui qu'on affiche)
        neighbour = pages[i - 1 if i > 0 else 1] if target is page else page
    await neighbour.bring_to_front()
    browser.show_tab(neighbour)
    await target.close()
    return f"🗂️  onglet « {title} » fermé"
