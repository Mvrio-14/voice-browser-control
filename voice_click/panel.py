"""PANNEAU : une barre flottante par-dessus la page (inspirée de la « Flow Bar » de Wispr Flow).

On peut y taper une commande, et elle affiche en direct ce qui se passe : l'onde de la voix pendant
l'écoute, la transcription, la décision, le résultat. Plus besoin de jongler avec le terminal.

Comment elle parle avec Python :
  - page → Python : Playwright « expose » une fonction Python à la page (expose_binding). Quand on
    valide une commande, le JavaScript appelle window.__voiceClickSubmit(texte), qui arrive dans
    la file `commands` lue par main.py.
  - Python → page : show() et level() appellent window.__voiceClickPanel en JavaScript.

La barre vit dans un « shadow DOM » : un mini-document isolé à l'intérieur de la page. Le CSS du
site ne la déforme pas, notre CSS ne touche pas au site, et see.py ne la voit pas (ni numérotée,
ni dans le texte visible). see.capture() la masque aussi sur les captures envoyées au modèle.
"""
import asyncio
import contextlib
import json

commands = asyncio.Queue()  # les commandes tapées dans la barre, lues par main.py

HOST_ID = "__voice_click_panel"

CSS = """
:host { all: initial; }
.wrap { display: flex; flex-direction: column; align-items: center; gap: 10px;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }

/* La bulle de statut, au-dessus de la barre */
.bubble { max-width: 460px; padding: 9px 15px; border-radius: 16px; font-size: 13px; line-height: 1.4;
  color: #f4f4f5; background: rgba(18, 18, 22, .78); border: 1px solid rgba(255, 255, 255, .08);
  backdrop-filter: blur(24px) saturate(170%); -webkit-backdrop-filter: blur(24px) saturate(170%);
  box-shadow: 0 10px 30px rgba(0, 0, 0, .28); pointer-events: none;
  opacity: 0; transform: translateY(6px) scale(.97); transition: opacity .25s ease, transform .25s ease; }
.bubble.visible { opacity: 1; transform: none; }
.bubble .label { display: block; font-size: 11px; letter-spacing: .02em; color: rgba(244, 244, 245, .5);
  margin-bottom: 2px; text-transform: uppercase; }

/* La barre (« pill ») */
.pill { display: flex; align-items: center; gap: 10px; width: 440px; height: 50px; box-sizing: border-box;
  padding: 0 10px 0 9px; border-radius: 999px; cursor: grab; user-select: none;
  background: rgba(18, 18, 22, .78); border: 1px solid rgba(255, 255, 255, .09);
  backdrop-filter: blur(24px) saturate(170%); -webkit-backdrop-filter: blur(24px) saturate(170%);
  box-shadow: 0 14px 44px rgba(0, 0, 0, .36), inset 0 1px 0 rgba(255, 255, 255, .06);
  transition: width .35s cubic-bezier(.2, .8, .2, 1), box-shadow .3s ease; }
.pill:active { cursor: grabbing; }
.pill:focus-within { box-shadow: 0 14px 44px rgba(0, 0, 0, .36), 0 0 0 1px rgba(167, 139, 250, .55); }

.input { flex: 1; min-width: 0; border: 0; outline: 0; background: transparent; color: #f4f4f5;
  font-size: 14px; font-family: inherit; cursor: text; }
.input::placeholder { color: rgba(244, 244, 245, .4); }

.kbd { flex: none; font-size: 11px; font-weight: 600; color: rgba(244, 244, 245, .55); padding: 3px 7px;
  border-radius: 7px; border: 1px solid rgba(255, 255, 255, .13); background: rgba(255, 255, 255, .04); }

/* L'orbe à gauche : son apparence dit l'état */
.orb { position: relative; flex: none; width: 32px; height: 32px; border-radius: 50%; cursor: pointer;
  display: flex; align-items: center; justify-content: center; gap: 2.5px;
  background: radial-gradient(circle at 32% 28%, #5b5b66, #2a2a31 70%);
  transition: background .3s ease, box-shadow .3s ease; }
.bar { width: 3px; height: 4px; border-radius: 3px; background: #fff; opacity: .9; transition: height .08s linear; }
.orb.idle .bar { height: 3px; opacity: .35; }

.orb.listening { background: linear-gradient(135deg, #a78bfa, #f472b6);
  box-shadow: 0 0 22px 2px rgba(244, 114, 182, .45); }

.orb.thinking { background: conic-gradient(from 0deg, #a78bfa, #f472b6, #60a5fa, #a78bfa);
  animation: spin 1.1s linear infinite; }
.orb.thinking .bar { opacity: 0; }

.orb.moving { background: linear-gradient(135deg, #60a5fa, #a78bfa); animation: pulse 1.4s ease-in-out infinite; }
.orb.moving .bar { opacity: 0; }
.orb.moving::after { content: "↻"; color: #fff; font-weight: 600; font-size: 15px; line-height: 1; position: absolute; }

.orb.done { background: linear-gradient(135deg, #34d399, #059669); box-shadow: 0 0 18px rgba(16, 185, 129, .4); }
.orb.done .bar, .orb.error .bar { opacity: 0; }
.orb.done::after { content: "✓"; color: #fff; font-weight: 700; font-size: 15px; line-height: 1; position: absolute; }
.orb.error { background: linear-gradient(135deg, #fb7185, #e11d48); }
.orb.error::after { content: "?"; color: #fff; font-weight: 700; font-size: 15px; line-height: 1; position: absolute; }

@keyframes spin { to { transform: rotate(360deg); } }
@keyframes pulse { 50% { box-shadow: 0 0 0 7px rgba(96, 165, 250, .0), 0 0 22px rgba(96, 165, 250, .5); } }

/* Mode compact : un clic sur l'orbe ne laisse qu'elle */
.wrap.compact .pill { width: 52px; padding: 0 9px; }
.wrap.compact .input, .wrap.compact .kbd { display: none; }

@media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; } }
"""

# Ce JavaScript est injecté dans CHAQUE page (add_init_script), avant même les scripts du site.
PANEL_JS = """
(() => {
  if (window.top !== window) return;  // pas dans les iframes

  const mount = () => {
    if (document.getElementById('HOST_ID')) return;
    const make = (tag, cls) => { const e = document.createElement(tag); if (cls) e.className = cls; return e; };

    const host = make('div');
    host.id = 'HOST_ID';
    host.style.cssText = 'position:fixed; left:50%; bottom:24px; transform:translateX(-50%); z-index:2147483647;';
    const root = host.attachShadow({ mode: 'open' });
    const style = make('style');
    style.textContent = CSS_TEXT;

    const wrap = make('div', 'wrap');
    const bubble = make('div', 'bubble');
    const pill = make('div', 'pill');
    const orb = make('div', 'orb idle');
    const bars = [0, 1, 2, 3, 4].map(() => orb.appendChild(make('span', 'bar')));
    const input = make('input', 'input');
    input.placeholder = 'Tape une commande…';
    input.spellcheck = false;
    const kbd = make('div', 'kbd');
    kbd.textContent = 'HOTKEY_LABEL';
    pill.append(orb, input, kbd);
    wrap.append(bubble, pill);
    root.append(style, wrap);
    document.documentElement.appendChild(host);

    // Les touches tapées dans la barre ne doivent pas déclencher les raccourcis du site (« k » = pause sur YouTube).
    for (const type of ['keydown', 'keyup', 'keypress']) input.addEventListener(type, e => e.stopPropagation());
    input.addEventListener('keydown', e => {
      if (e.key === 'Enter' && input.value.trim()) {
        window.__voiceClickSubmit(input.value.trim());
        input.value = '';
        input.blur();  // sinon « valide » ou « écris X » enverraient les touches dans la barre, pas dans la page
      }
      if (e.key === 'Escape') input.blur();
    });

    // Déplacer la barre en la faisant glisser ; un simple clic sur l'orbe la réduit / l'agrandit.
    let drag = null;
    pill.addEventListener('pointerdown', e => {
      if (e.target === input) return;
      const r = host.getBoundingClientRect();
      drag = { dx: e.clientX - r.left, dy: e.clientY - r.top, moved: false };
      pill.setPointerCapture(e.pointerId);
    });
    pill.addEventListener('pointermove', e => {
      if (!drag) return;
      drag.moved = true;
      Object.assign(host.style, { left: (e.clientX - drag.dx) + 'px', top: (e.clientY - drag.dy) + 'px',
                                  bottom: 'auto', transform: 'none' });
    });
    pill.addEventListener('pointerup', e => {
      if (drag && !drag.moved && orb.contains(e.target)) wrap.classList.toggle('compact');
      drag = null;
    });

    let hideTimer = null;
    let rest = 'idle';  // l'état de repos : idle (veille) ou listening (session ouverte)
    window.__voiceClickPanel = {
      // state : idle | listening | thinking | moving | done | error ; label + text : la bulle
      show(state, label, text) {
        orb.className = 'orb ' + state;
        if (state === 'idle' || state === 'listening') rest = state;
        clearTimeout(hideTimer);
        bubble.replaceChildren();
        if (text) {
          const l = make('span', 'label');
          l.textContent = label || '';
          bubble.append(l, document.createTextNode(text));
          bubble.classList.add('visible');
        } else {
          bubble.classList.remove('visible');
        }
        if (state === 'done' || state === 'error') {
          hideTimer = setTimeout(() => { bubble.classList.remove('visible'); orb.className = 'orb ' + rest; }, 4500);
        }
      },
      // le niveau sonore du micro (0 à 1) : l'onde monte quand on parle, reste basse dans le silence
      level(v) {
        bars.forEach((b, i) => {
          const shape = [0.55, 0.85, 1, 0.85, 0.55][i];
          b.style.height = (4 + Math.min(1, v) * 16 * shape * (0.75 + Math.random() * 0.5)) + 'px';
        });
      },
    };
  };

  if (document.documentElement) mount();
  document.addEventListener('DOMContentLoaded', mount);
})();
""".replace("HOST_ID", HOST_ID).replace("CSS_TEXT", json.dumps(CSS))


def panel_js(hint):
    """Le JavaScript de la barre, avec à droite comment lui parler (ex : « Hey browser » ou Ctrl+Maj+Espace)."""
    return PANEL_JS.replace("HOTKEY_LABEL", hint)


async def install(context, hint):
    """À appeler une fois, avant d'ouvrir la première page."""
    await context.expose_binding("__voiceClickSubmit", lambda source, text: commands.put_nowait(text))
    await context.add_init_script(panel_js(hint))


async def next_command():
    return await commands.get()


async def show(page, state, label="", text=""):
    """Met à jour la barre. state : idle | listening | thinking | moving | done | error."""
    with contextlib.suppress(Exception):  # (page en cours de chargement : tant pis pour cette mise à jour)
        await page.evaluate("([s, l, t]) => window.__voiceClickPanel?.show(s, l, t)", [state, label, text])


async def level(page, value):
    """Le niveau sonore du micro (0 à 1), pour animer l'onde."""
    with contextlib.suppress(Exception):
        await page.evaluate("v => window.__voiceClickPanel?.level(v)", value)
