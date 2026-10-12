"""MOUVEMENTS CONTINUS : faire un mouvement en continu, jusqu'à « stop » ou jusqu'à une condition.

Trois concepts indépendants, décidés par le modèle dans decide.py :
  - le MOUVEMENT : défiler, avancer / reculer la vidéo, zoomer / dézoomer
  - la DURÉE     : une fois (act.py s'en occupe) · jusqu'à « stop » · jusqu'à une condition
  - la CONDITION : recopiée de la phrase (« une vidéo avec un chat sur la miniature », « la minute 5 »)
N'importe quel mouvement se combine avec n'importe quelle condition : « recule jusqu'à ce qu'on voie
la voiture rouge », « zoome jusqu'à ce que le texte soit lisible »... sans code spécifique.

Le mouvement tourne en tâche de fond : main.py continue d'écouter pendant ce temps.
Garde-fous, pour un arrêt robuste :
  1. condition confirmée DEUX fois : quand le modèle pense l'avoir vue, on s'arrête et on revérifie sur
     l'écran immobile (image nette, pas de dépassement). Fausse alerte → on repart.
  2. fin naturelle : plus rien ne bouge (bas de page, fin de la vidéo, zoom maximal)
  3. sécurité : un nombre maximum de vérifications
  4. la voix de l'utilisateur arrête tout, immédiatement (main.py appelle stop())
"""
import asyncio
import contextlib

import panel
import see as eyes
from decide import condition_met

MOTIONS = {"scroll_down", "scroll_up", "video_forward", "video_back", "zoom_in", "zoom_out"}

# Vitesses « jusqu'à stop » : on parcourt le contenu, donc à une vitesse de lecture.
SCROLL_SPEED = {"un peu": 60, "normal": 120, "beaucoup": 250, "au maximum": 400}  # pixels par seconde
VIDEO_SPEED = {"un peu": 4, "normal": 10, "beaucoup": 30, "au maximum": 60}  # secondes de vidéo par seconde
# « Jusqu'à condition » : on cherche quelque chose, donc plus vite.
SEARCH_SCROLL_SPEED = 300  # la page défile en continu pendant les vérifications
# La vidéo, elle, avance PAR SAUTS : chaque saut la fait recharger, et vérifier pendant le chargement
# reviendrait à regarder un écran noir. Donc : un saut, on attend que l'image soit prête, on vérifie.
SEARCH_VIDEO_STEP = 10  # secondes par saut (40 vérifications max → ~6 minutes de vidéo parcourues)
SEEK_JS = f"""s => {{ const v = {eyes.MAIN_VIDEO}; if (v) v.currentTime = Math.max(0, Math.min(v.duration, v.currentTime + s)); }}"""

FOUND = 0.8  # probabilité au-dessus de laquelle la condition est considérée remplie
MAX_CHECKS = 40  # sécurité : on abandonne la recherche après 40 vérifications
TICK = 0.5  # sans condition : on regarde toutes les 0,5 s si ça bouge encore (fin de page...)
# En dessous de ce déplacement entre deux vérifications, on considère que plus rien ne bouge
# (une vidéo qui joue à 0:00 bouge de quelques centièmes : ce n'est pas un vrai mouvement).
STILL = {"scroll": 5, "video": 1, "zoom": 0.01}  # pixels, secondes, niveau de zoom

# Les animations tournent DANS la page (pas d'aller-retour avec Python) : défilement à ~60 images/s
# (en accumulant les fractions de pixel, pour que les vitesses lentes restent fluides) et avance /
# recul de la vidéo toutes les 250 ms. speed est négatif pour remonter / reculer.
START_JS = f"""([kind, speed]) => {{
  cancelAnimationFrame(window.__voiceClickScroll);
  clearInterval(window.__voiceClickVideo);
  if (kind === 'scroll') {{
    let last = performance.now(), pending = 0;
    const tick = now => {{
      pending += speed * (now - last) / 1000;
      last = now;
      const px = Math.trunc(pending);
      if (px !== 0) {{ window.scrollBy(0, px); pending -= px; }}
      window.__voiceClickScroll = requestAnimationFrame(tick);
    }};
    window.__voiceClickScroll = requestAnimationFrame(tick);
  }}
  if (kind === 'video') {{
    window.__voiceClickVideo = setInterval(() => {{
      const v = {eyes.MAIN_VIDEO};
      if (v) v.currentTime = Math.max(0, Math.min(v.duration, v.currentTime + speed / 4));
    }}, 250);
  }}
}}"""
STOP_JS = "() => { cancelAnimationFrame(window.__voiceClickScroll); clearInterval(window.__voiceClickVideo); }"
POSITION_JS = f"""kind => kind === 'scroll' ? scrollY : (({eyes.MAIN_VIDEO}) || {{}}).currentTime"""

task = None  # le mouvement en cours (tâche de fond), ou None


def running():
    return task is not None and not task.done()


async def stop(page):
    """Arrête le mouvement en cours. Appelé dès que l'utilisateur parle ou tape une commande."""
    if running():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def start(page, action, amount, condition, request, until_stop):
    """Lance le mouvement en tâche de fond : jusqu'à « stop » (until_stop), ou jusqu'à ce que la condition
    soit remplie (condition = l'affirmation à vérifier, ou None pour juger sur la phrase complète)."""
    global task
    await stop(page)
    task = asyncio.create_task(run(page, action, amount, condition, request, until_stop))


async def run(page, action, amount, condition, request, until_stop):
    kind = action.split("_")[0]  # "scroll", "video" ou "zoom"
    sign = 1 if action in ("scroll_down", "video_forward", "zoom_in") else -1
    # Deux façons d'avancer : en continu (animé dans la page), ou par paliers / sauts à chaque tour
    # (le zoom ; la vidéo quand on cherche quelque chose, pour ne pas vérifier pendant le chargement).
    stepped = kind == "zoom" or (kind == "video" and not until_stop)
    speed = (SCROLL_SPEED if kind == "scroll" else VIDEO_SPEED)[amount] if until_stop else SEARCH_SCROLL_SPEED
    goal = condition or request  # (pour l'affichage)

    async def move():  # (re)démarre le mouvement continu
        if not stepped:
            await page.evaluate(START_JS, [kind, sign * speed])

    async def step():  # un palier / un saut
        if kind == "zoom":
            await eyes.change_zoom(page, sign)
        elif stepped:
            await page.evaluate(SEEK_JS, sign * SEARCH_VIDEO_STEP)
            await eyes.wait_video_ready(page)

    async def position():
        return eyes.zoom if kind == "zoom" else await page.evaluate(POSITION_JS, kind)

    try:
        await move()
        last = None
        for _ in range(10**6 if until_stop else MAX_CHECKS):
            await step()

            if until_stop:
                await asyncio.sleep(TICK)
            elif await is_met(page, condition, request):
                await page.evaluate(STOP_JS)  # on s'arrête, puis on revérifie sur l'écran immobile
                await asyncio.sleep(0.2)
                if await is_met(page, condition, request):
                    print(f"\n✅ Condition remplie : « {goal} »")
                    await panel.show(page, "done", "Trouvé", goal)
                    return
                await move()  # fausse alerte : on repart

            now = await position()  # (None si la vidéo a disparu un instant : pub, chargement...)
            if None not in (now, last) and abs(now - last) < STILL[kind]:  # plus rien ne bouge : bas de page, fin de la vidéo, zoom maximal
                print("\n⏹️  Arrivé au bout" + ("" if until_stop else f" sans voir « {goal} »"))
                await panel.show(page, "done" if until_stop else "error", "Arrivé au bout",
                                 "Fin atteinte" if until_stop else f"Pas trouvé : {goal}")
                return
            last = now
        print(f"\n🤷 « {goal} » toujours pas remplie après {MAX_CHECKS} vérifications : j'arrête")
        await panel.show(page, "error", "Abandon", f"Pas trouvé : {goal}")
    except Exception as error:  # (une tâche de fond qui plante en silence, c'est le pire : on le dit)
        print(f"\n💥 Le mouvement s'est arrêté sur une erreur : {error!r}")
        await panel.show(page, "error", "Erreur", "Le mouvement s'est arrêté sur une erreur")
    finally:  # condition remplie, bout atteint, abandon ou interruption par l'utilisateur
        with contextlib.suppress(Exception):  # (la page a pu changer ou se fermer entre-temps)
            await page.evaluate(STOP_JS)


async def is_met(page, condition, request):
    """Demande au modèle si la condition d'arrêt est remplie, avec toutes les preuves : image, texte visible,
    état, et la phrase complète comme contexte."""
    screenshot = await eyes.capture(page, quality=60)  # (sans notre barre, sans effet visible)
    text = await eyes.visible_text(page)
    state = await eyes.page_state(page)
    # (dans un thread : l'appel réseau ne doit pas bloquer l'écoute du micro pendant ce temps)
    return await asyncio.to_thread(condition_met, request, condition, screenshot, text, state) > FOUND
