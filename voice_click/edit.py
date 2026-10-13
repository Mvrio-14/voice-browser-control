"""ÉCRIRE : modifier le texte d'un champ, comme un humain au clavier.

Deux étapes, deux outils :
1. QUOI écrire : un petit LLM rapide (gpt-5.4-mini) reçoit le champ (son libellé, son type, son contenu
   actuel) et la phrase transcrite. Il renvoie le NOUVEAU contenu complet du champ. Écrire, compléter,
   effacer, remplacer des lettres, épeler, changer la casse : tout passe par là. Le modèle de décision,
   lui, sait choisir mais pas écrire du texte nouveau (« J-U-L-E-T » → « julet »).
2. COMMENT l'écrire : le code compare l'ancien et le nouveau contenu, et ne tape que la différence :
   curseur à la fin (Ctrl+Fin), flèches vers la gauche jusqu'à la partie qui change, Retour arrière pour
   l'effacer, puis le nouveau texte caractère par caractère. Le site reçoit de vraies frappes clavier
   (suggestions, vérifications en direct... fonctionnent comme si tu tapais).

On garde l'ancien contenu : « annule » le remet.
Les champs mot de passe ne sont jamais lus ni écrits (et dire son mot de passe à voix haute n'est pas une bonne idée).
"""
import asyncio
import json

from openai import OpenAI

# Mesuré sur 14 éditions (épeler, remplacer des lettres, effacer un mot...) : gpt-5.4-nano 8/14 (0,7 s),
# nano avec un peu de réflexion 10/14 (1,2 s), gpt-5.4-mini avec un peu de réflexion 13/14 (1,2 s).
# Le nano se trompe sur les manipulations au caractère près ; le mini reste très bon marché (< 0,1 centime par édition).
MODEL = "gpt-5.4-mini"
REASONING = "low"
TYPING_DELAY_MS = 15  # entre deux caractères tapés

client = OpenAI()
last_edit = None  # (le champ modifié, son contenu d'avant) : pour « annule »

INSTRUCTIONS = """Tu aides une personne qui pilote son navigateur à la voix. Elle veut modifier le contenu d'un champ de texte.
Sa phrase vient d'une transcription automatique, souvent imparfaite (bruit, mots mal reconnus).

Renvoie UNIQUEMENT le nouveau contenu complet du champ, tel qu'il doit apparaître, sans guillemets ni explication.
- Garde seulement ce qu'elle veut voir écrit, sans les mots de la commande (« écris », « dans le champ », « ajoute »...).
- Selon sa demande : tout remplacer, ajouter à la suite, effacer (tout ou une partie), corriger, changer la casse...
- Les lettres épelées (« J-U-L-E-T », « j u l », « J comme Jacques ») s'écrivent attachées.
- Les symboles dits à l'oral s'écrivent en symboles (« arobase » → @, « point » → ., « tiret » → -, « tiret du bas » → _).
- Sers-toi du type et du libellé du champ pour corriger les erreurs probables de transcription."""

# Le champ où se trouve le curseur (celui qu'on vient de cliquer, ou déjà sélectionné).
FIELD_JS = """() => {
  const el = document.activeElement;
  if (!el) return null;
  const tag = el.tagName.toLowerCase();
  const editable = el.isContentEditable;
  const textInput = tag === 'textarea' || (tag === 'input' &&
    !['button', 'submit', 'checkbox', 'radio', 'file', 'image', 'range', 'color', 'reset', 'hidden'].includes(el.type));
  if (!editable && !textInput) return null;
  if (el.type === 'password') return {password: true};
  const label = el.getAttribute('aria-label') || (el.labels && el.labels[0] && el.labels[0].innerText)
    || el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('name') || '';
  return {kind: editable ? 'texte riche' : tag, type: editable ? '' : (el.type || ''), label: label.trim().slice(0, 100),
          value: editable ? el.innerText : el.value};
}"""


def rewrite(field, request, history=()):
    """Étape 1 : le nouveau contenu du champ, d'après la phrase."""
    state = {
        "champ": {"libellé": field["label"], "type": field["type"] or field["kind"]},
        "contenu actuel": field["value"],
        "ce qu'elle a demandé juste avant": list(history),
        "ce qu'elle dit maintenant": request,
    }
    response = client.responses.create(model=MODEL, instructions=INSTRUCTIONS, reasoning={"effort": REASONING},
                                       input=json.dumps(state, ensure_ascii=False))
    return response.output_text.strip()


async def type_difference(page, old, new):
    """Étape 2 : tape seulement ce qui change, comme un humain. Ex : « julet@gmail.co » → « julet@gmail.com » :
    curseur à la fin, puis « m »."""
    start = 0  # la partie commune au début...
    while start < min(len(old), len(new)) and old[start] == new[start]:
        start += 1
    end = 0  # ... et à la fin
    while end < min(len(old), len(new)) - start and old[-1 - end] == new[-1 - end]:
        end += 1
    await page.keyboard.press("Control+End")  # le curseur tout à la fin du champ
    for _ in range(end):  # puis juste après la partie qui change
        await page.keyboard.press("ArrowLeft")
    for _ in range(len(old) - start - end):  # on l'efface
        await page.keyboard.press("Backspace")
    await page.keyboard.type(new[start:len(new) - end], delay=TYPING_DELAY_MS)  # et on tape la nouvelle


async def edit(page, request, history=()):
    """Modifie le champ où se trouve le curseur, d'après la phrase. Renvoie ce qui a été fait."""
    global last_edit
    field = await page.evaluate(FIELD_JS)
    if field is None:
        return "🤷 rien fait (aucun champ de texte sélectionné)"
    if field.get("password"):
        return "🔒 rien fait (je n'écris pas les mots de passe : tape-le toi-même)"
    new = await asyncio.to_thread(rewrite, field, request, history)
    if new == field["value"]:
        return f"🤷 champ inchangé (« {shorten(new)} »)"
    last_edit = (await page.evaluate_handle("document.activeElement"), field["value"])
    await type_difference(page, field["value"], new)
    return f"⌨️  « {shorten(field['value'])} » → « {shorten(new)} »" if field["value"] else f"⌨️  écrit « {shorten(new)} »"


async def undo(page):
    """Remet le dernier champ modifié comme avant."""
    global last_edit
    if last_edit is None:
        return "🤷 rien à annuler"
    element, before = last_edit
    try:
        await element.focus()
        now = await page.evaluate(FIELD_JS)
    except Exception:  # (la page a changé : le champ n'existe plus)
        last_edit = None
        return "🤷 rien à annuler (le champ n'est plus là)"
    await type_difference(page, now["value"], before)
    last_edit = None
    return f"↩️  champ remis à « {shorten(before)} »"


def shorten(text, n=60):
    return text if len(text) <= n else text[:n - 1] + "…"
