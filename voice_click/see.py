"""YEUX : numéroter les éléments visibles à l'écran et prendre une capture (technique « Set-of-Marks »).

Le modèle de décision ne sait pas donner des coordonnées : il choisit dans une liste.
On dessine donc un numéro sur chaque élément cliquable, on prend la capture, et le modèle
n'a plus qu'à répondre « 12 ». Le code connaît la position de l'élément 12 et clique dessus.
"""

import base64
import io

from PIL import Image, ImageDraw
from playwright.async_api import Error as PlaywrightError

import panel

# Ce JavaScript s'exécute DANS la page web.
MARK_JS = """
() => {
  const SELECTOR = 'a[href], button, input, textarea, select, img, ' +
                   '[role=button], [role=link], [role=tab], [role=menuitem], [role=checkbox], [onclick]';
  // Le cadre d'un élément. Deux cas particuliers :
  //  - un lien coupé sur 2 lignes a 2 rectangles : on prend le 1er (sinon son centre tombe à côté du lien)
  //  - un lien qui contient une image : on prend le cadre de l'image
  // (gardée dans window pour que act.py remesure la position juste avant de cliquer)
  window.__voiceClickBox = el => {
    const img = el.tagName === 'A' && el.querySelector('img');
    return img ? img.getBoundingClientRect() : el.getClientRects()[0];
  };

  // Dessine un cadre + son numéro juste AU-DESSUS du cadre, pour ne pas cacher le texte de l'élément
  // (pointer-events:none : les dessins n'empêchent pas de cliquer ; pas d'innerHTML : YouTube l'interdit)
  const draw = (r, n) => {
    const mark = document.createElement('div');
    mark.className = '__voice_click_mark';
    mark.style.cssText = `position:fixed; left:${r.left}px; top:${r.top}px; width:${r.width}px; height:${r.height}px;
      border:2px solid #e11d48; z-index:2147483647; pointer-events:none; box-sizing:border-box;`;
    const label = document.createElement('span');
    label.textContent = n;
    label.style.cssText = `position:absolute; left:-2px; top:${r.top < 14 ? 0 : -14}px; background:#e11d48;
      color:white; font:bold 11px/12px sans-serif; padding:1px 3px;`;
    mark.appendChild(label);
    document.body.appendChild(mark);
  };

  // ---- La description d'un élément : son RÔLE, son NOM et son ÉTAT, comme pour un lecteur d'écran ----
  // (c'est la logique standard de l'« arbre d'accessibilité » des navigateurs, en version simplifiée)
  const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
  const shorten = (s, max = 100) => s.length <= max ? s : s.slice(0, s.lastIndexOf(' ', max) > 40 ? s.lastIndexOf(' ', max) : max) + '…';
  const bigImage = el => {
    const img = el.querySelector && el.querySelector('img');
    const r = img && img.getBoundingClientRect();
    return r && r.width * r.height >= 80 * 80;
  };
  const isSearch = el => /search|recherch/i.test([el.type, el.name, el.placeholder, el.getAttribute('aria-label')].join(' '));

  // 1. Le RÔLE : ce qu'est l'élément, en français lisible
  const ARIA_ROLES = { button: 'bouton', link: 'lien', tab: 'onglet', menuitem: 'élément de menu',
                       checkbox: 'case à cocher', radio: 'choix', switch: 'interrupteur', option: 'option',
                       searchbox: 'champ de recherche', textbox: 'champ de texte' };
  const roleOf = el => {
    const tag = el.tagName, type = (el.type || '').toLowerCase();
    if (tag === 'INPUT') {
      if (type === 'checkbox') return 'case à cocher';
      if (type === 'radio') return 'choix';
      if (['submit', 'button', 'reset', 'image'].includes(type)) return 'bouton';
      return isSearch(el) ? 'champ de recherche' : 'champ de texte';
    }
    if (tag === 'TEXTAREA') return 'champ de texte';
    if (tag === 'SELECT') return 'liste déroulante';
    if (tag === 'IMG') return 'image';
    if (ARIA_ROLES[el.getAttribute('role')]) return ARIA_ROLES[el.getAttribute('role')];
    if (tag === 'A') return bigImage(el) ? 'lien (miniature)' : 'lien';
    if (tag === 'BUTTON' || tag === 'SUMMARY') return 'bouton';
    return 'élément cliquable';
  };

  // 2. Le NOM : la première source qui existe, dans l'ordre standard. (On lit les ATTRIBUTS HTML, pas les
  //    propriétés JavaScript : certains sites, comme YouTube, redéfinissent el.title.) On note aussi son rang :
  //    un nom « prévu pour ça » (aria-label, étiquette, alt...) vaut mieux que le texte affiché.
  const nameOf = el => {
    const labelledBy = (el.getAttribute('aria-labelledby') || '').split(' ')
      .map(id => document.getElementById(id)).filter(Boolean).map(e => e.innerText).join(' ');
    const img = el.tagName === 'A' && el.querySelector('img');
    const sources = [
      labelledBy,                                            // 1. aria-labelledby : « mon nom est écrit là »
      el.getAttribute('aria-label'),                         // 2. aria-label : le nom pour les lecteurs d'écran
      [...(el.labels || [])].map(l => l.innerText).join(' '), // 3. l'étiquette <label> d'un champ
      el.getAttribute('alt') || (img && img.getAttribute('alt')),  // 4. le texte alternatif d'une image
      el.getAttribute('title'),                              // 5. l'infobulle
      ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName) ? '' : el.innerText,  // 6. le texte affiché
      el.getAttribute('placeholder'),                        // 7. le texte d'exemple d'un champ
    ];
    for (let rank = 0; rank < sources.length; rank++) {
      const name = clean(sources[rank]);
      if (name) return { name: shorten(name), rank };
    }
    return { name: '', rank: sources.length };
  };

  // 3. L'ÉTAT : ce qu'il contient ou s'il est coché (jamais le contenu d'un champ de mot de passe)
  const stateOf = el => {
    const type = (el.type || '').toLowerCase();
    if (type === 'checkbox' || type === 'radio') return el.checked ? 'coché' : 'non coché';
    if (el.tagName === 'SELECT') return 'valeur : ' + clean(el.selectedOptions[0]?.text);
    if (['INPUT', 'TEXTAREA'].includes(el.tagName) && type !== 'password' && el.value) return 'contient « ' + shorten(clean(el.value), 60) + ' »';
    if (el.getAttribute('aria-checked') === 'true' || el.getAttribute('aria-selected') === 'true') return 'sélectionné';
    if (el.getAttribute('aria-expanded') === 'true') return 'ouvert';
    return '';
  };

  const kept = new Set();
  const elements = [];
  window.__voiceClickElements = [];  // l'élément n°12 est en position 11 : act.py s'en sert pour cliquer

  for (const el of document.querySelectorAll(SELECTOR)) {
    const box = window.__voiceClickBox(el);
    if (!box || box.width < 8 || box.height < 8) continue;

    // 1. Visible à l'écran ? On ne garde que la PARTIE VISIBLE de l'élément : une miniature coupée par le bas
    //    de la fenêtre doit rester choisissable (elle est sous les yeux de l'utilisateur), même si son
    //    centre est hors de l'écran. Il faut juste qu'un morceau suffisant soit visible.
    const left = Math.max(box.left, 0), top = Math.max(box.top, 0);
    const right = Math.min(box.right, innerWidth), bottom = Math.min(box.bottom, innerHeight);
    if (right - left < Math.min(24, box.width) || bottom - top < Math.min(24, box.height)) continue;
    const r = { left, top, width: right - left, height: bottom - top };

    // 2. Pas caché derrière autre chose ? (ce qui est affiché au centre de sa partie visible doit être lui)
    const onTop = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    if (!onTop || !el.contains(onTop)) continue;

    // 3. Pas déjà couvert par un parent gardé ? (ex : l'image DANS un lien = un seul élément)
    let parent = el.parentElement, inside = false;
    while (parent) { if (kept.has(parent)) { inside = true; break; } parent = parent.parentElement; }
    if (inside) continue;

    const role = roleOf(el), { name, rank } = nameOf(el), state = stateOf(el);

    // 4. Un lien vers une page déjà numérotée ? (ex : la miniature ET le titre d'une même vidéo)
    //    C'est le même choix : on dessine le MÊME numéro sur les deux, et on garde le MEILLEUR nom
    //    (celui de plus haut rang : le titre prévu pour ça plutôt que « 10:51 Now playing » affiché sur
    //    la miniature). (Deux numéros différents = deux bonnes réponses : le modèle hésiterait.)
    const href = el.tagName === 'A' && el.href;
    const same = href && elements.find(e => e.href === href);
    if (same) {
      if (rank < same.rank) Object.assign(same, { name, rank });
      if (role === 'lien (miniature)') same.role = role;
      draw(r, same.n);
      continue;
    }

    kept.add(el);
    window.__voiceClickElements.push(el);
    const n = elements.length + 1;
    elements.push({ n, role, name, rank, state, href, x: r.left, y: r.top, w: r.width, h: r.height });
    draw(r, n);

    if (elements.length >= 80) break;  // au-delà, la capture devient illisible
  }
  return elements;
}
"""

UNMARK_JS = "() => document.querySelectorAll('.__voice_click_mark').forEach(m => m.remove())"

# Niveau de zoom voulu par l'utilisateur (1 = 100 %), modifié par act.py.
# Une nouvelle page repart à 100 % : on le réapplique donc avant chaque capture.
ZOOM_LEVELS = [0.5, 0.67, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0]  # les paliers de Chrome
zoom = 1.0

# La vidéo principale de la page : la plus grande, s'il y en a une (expression JS, réutilisée par act.py).
MAIN_VIDEO = ("[...document.querySelectorAll('video')].filter(v => v.duration > 0)"
              ".sort((a, b) => b.clientWidth * b.clientHeight - a.clientWidth * a.clientHeight)[0]")
VIDEO_STATE_JS = f"""() => {{
  const v = {MAIN_VIDEO};
  return v ? {{ paused: v.paused, muted: v.muted, volume: v.volume, time: v.currentTime, duration: v.duration,
               loading: v.seeking || v.readyState < 2 }} : null;  // (readyState < 2 : l'image n'est pas encore là)
}}"""

# Attend que la vidéo ait fini de charger après un saut (au plus 3 s) : sinon on regarderait l'écran
# de chargement au lieu de l'image.
WAIT_VIDEO_JS = f"""() => new Promise(resolve => {{
  const v = {MAIN_VIDEO}, start = performance.now();
  const check = () => (!v || (!v.seeking && v.readyState >= 2) || performance.now() - start > 3000)
    ? resolve() : setTimeout(check, 50);
  check();
}})"""


async def wait_video_ready(page):
    await page.evaluate(WAIT_VIDEO_JS)


def mmss(seconds):
    """192.4 → "3:12" """
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


# Le texte affiché à l'écran (titres, noms de chaînes, paragraphes...), dans l'ordre de la page.
# Pour reconnaître « une vidéo de Squeezie », le nom écrit est une preuve plus fiable qu'un visage.
VISIBLE_TEXT_JS = """() => {
  const parts = [], range = document.createRange();
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let node, total = 0;
  while ((node = walker.nextNode()) && total < 3000) {
    const text = node.textContent.replace(/\\s+/g, ' ').trim();
    if (!text || node.parentElement.closest('script, style, noscript')) continue;
    range.selectNodeContents(node);
    const r = range.getBoundingClientRect();  // ce texte est-il à l'écran ?
    if (r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth) {
      parts.push(text);
      total += text.length;
    }
  }
  return parts.join(' | ');
}"""


async def visible_text(page):
    return await page.evaluate(VISIBLE_TEXT_JS)


# Où en est-on dans la page ? (0 % = tout en haut, 100 % = tout en bas)
SCROLL_JS = """() => {
  const max = document.documentElement.scrollHeight - innerHeight;
  return max > 0 ? Math.round(100 * scrollY / max) : 100;
}"""


async def apply_zoom(page):
    # (la barre de panel.py est dans la page : on la « dézoome » d'autant pour qu'elle garde sa taille)
    await page.evaluate(f"""() => {{
      document.documentElement.style.zoom = {zoom};
      const bar = document.getElementById('{panel.HOST_ID}');
      if (bar) bar.style.zoom = 1 / {zoom};
    }}""")


async def change_zoom(page, steps):
    """Monte (steps > 0) ou descend (steps < 0) de quelques paliers de zoom, sans dépasser les limites."""
    global zoom
    i = min(range(len(ZOOM_LEVELS)), key=lambda k: abs(ZOOM_LEVELS[k] - zoom))  # le palier le plus proche
    zoom = ZOOM_LEVELS[max(0, min(i + steps, len(ZOOM_LEVELS) - 1))]
    await apply_zoom(page)


async def see(page):
    """Renvoie (éléments numérotés, capture JPEG avec les numéros, état de la page en une phrase).
    Si la page change pendant qu'on la regarde (chargement, redirection), on attend et on recommence une fois."""
    for attempt in (1, 2):
        try:
            await page.wait_for_load_state("domcontentloaded")
            await apply_zoom(page)
            elements = await page.evaluate(MARK_JS)
            screenshot = await capture(page)
            await page.evaluate(UNMARK_JS)
            return elements, screenshot, await page_state(page)
        except PlaywrightError:
            if attempt == 2:
                raise
            await page.wait_for_timeout(500)


cdp_sessions = {}  # une connexion directe au navigateur (protocole CDP) par onglet, réutilisée

PANEL_BOX_JS = f"""() => {{
  const bar = document.getElementById('{panel.HOST_ID}');
  const r = bar && bar.getBoundingClientRect();
  return [innerWidth, innerHeight, devicePixelRatio, r ? [r.left, r.top, r.right, r.bottom] : null];
}}"""


async def capture(page, quality=70):
    """Une capture d'écran JPEG, sans AUCUN effet visible pour l'utilisateur.
    page.screenshot() de Playwright cachait la barre (clignotement) et redimensionnait brièvement l'affichage
    sur les écrans mis à l'échelle (mouvements). Ici : capture directe via le protocole de Chrome (CDP),
    puis, en Python, on masque la zone de la barre sur l'IMAGE (pas dans la page) et on la ramène à la
    taille de la page en pixels CSS (sinon, à 200 % de mise à l'échelle Windows, 4 fois plus d'image à envoyer)."""
    if page not in cdp_sessions:
        cdp_sessions[page] = await page.context.new_cdp_session(page)
    shot = await cdp_sessions[page].send("Page.captureScreenshot", {"format": "jpeg", "quality": 90})
    width, height, ratio, bar = await page.evaluate(PANEL_BOX_JS)

    image = Image.open(io.BytesIO(base64.b64decode(shot["data"]))).convert("RGB")
    if bar:  # un rectangle neutre là où est la barre (avec une marge, pour l'ombre)
        left, top, right, bottom = (v * ratio for v in bar)
        ImageDraw.Draw(image).rectangle([left - 8, top - 8, right + 8, bottom + 8], fill=(128, 128, 128))
    image = image.resize((round(width), round(height)))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=quality)
    return out.getvalue()


async def page_state(page):
    """L'état de la page en une phrase : où on en est (page, zoom, position, vidéo).
    Sans lui, « au max », « encore » ou « jusqu'à la minute 5 » sont difficiles à interpréter."""
    scroll = await page.evaluate(SCROLL_JS)
    state = (
        f"Page : « {await page.title()} » ({page.url}). "
        f"Zoom : {zoom:.0%} (possible de {ZOOM_LEVELS[0]:.0%} à {ZOOM_LEVELS[-1]:.0%}). "
        f"Position dans la page : {scroll} % (0 % = tout en haut, 100 % = tout en bas)."
    )
    v = await page.evaluate(VIDEO_STATE_JS)
    if v:
        length = "en direct" if v["duration"] == float("inf") else f"sur {mmss(v['duration'])}"
        sound = "coupé" if v["muted"] else f"activé, volume {v['volume']:.0%}"
        playing = "en chargement" if v["loading"] else "en pause" if v["paused"] else "en lecture"
        state += f" Vidéo : {playing} à {mmss(v['time'])} {length}, son {sound}."
    return state
