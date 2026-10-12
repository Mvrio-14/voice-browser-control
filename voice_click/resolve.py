"""CIBLE : sur quel élément agir ? En deux temps : présélectionner, puis départager.

Pourquoi ? decide.py choisit la cible parmi TOUS les éléments numérotés (souvent 30 à 60), en une seule
question. C'est rapide, mais fragile dès que la demande décrit ce qu'on VOIT (« la vidéo avec le chat ») :
  - le modèle se fie aux textes des options (« LLM from Scratch », dont la mascotte est un chat...) ;
  - il associe mal un petit numéro à son contenu quand beaucoup d'éléments se ressemblent ;
  - si l'élément demandé n'est pas à l'écran, il choisit « ce qui ressemble » au lieu de « aucun ».

Donc, pour toute action qui a une cible (clic, recherche dans le site, écriture) :
  1. PRÉSÉLECTION : les SHORTLIST candidats les plus probables selon decide.py ;
  2. DÉPARTAGE : UNE question à choix parmi ces seuls candidats, chacun montré en gros plan avec sa
     description, plus « aucun ». On garde la COMPARAISON (« le logo rouge » plutôt que le noir, « sans le
     chat »), sans l'encombrement des 40 numéros. (Juger chaque candidat seul, en oui/non, a été essayé et
     mesuré : on perdait justement la comparaison. Voir eval/.)
"""
import base64
import io

from PIL import Image

from decide import MODEL, client, describe

TARGET_ACTIONS = {"click", "search", "type_text"}  # les actions qui agissent sur un élément
SHORTLIST = 4  # nombre de candidats départagés
MIN_SHORTLIST_P = 0.02  # (un candidat à moins de 2 % n'est pas présélectionné)
CONFIRMED = 0.5  # confiance minimale du départage pour agir
MARGIN = 12  # pixels autour de l'élément sur le gros plan (un peu de contexte visuel)

# La question dépend de l'action : on clique sur un élément, mais on écrit DANS un champ
# (que la personne ne nomme souvent pas : « cherche lofi girl »).
QUESTION = {
    "click": "Sur lequel de ces éléments faut-il cliquer pour faire ce qu'elle demande ?",
    "search": "Dans lequel de ces éléments faut-il écrire pour faire la recherche qu'elle demande ?",
    "type_text": "Dans lequel de ces éléments faut-il écrire le texte qu'elle demande ?",
}


def resolve(request, d, elements, screenshot):
    """Renvoie la décision d avec la cible départagée : "target" (ou None), "target_confidence", "shortlist"."""
    if d["action"] not in TARGET_ACTIONS:
        return d
    by_number = {str(e["n"]): e for e in elements}
    ranked = sorted(d["target_probabilities"].items(), key=lambda kv: -kv[1])
    candidates = [by_number[n] for n, p in ranked if n in by_number and p >= MIN_SHORTLIST_P][:SHORTLIST]
    if not candidates:
        return {**d, "target": None, "target_confidence": 0.0, "shortlist": []}

    full = Image.open(io.BytesIO(screenshot))
    content = [{"type": "input_text", "text": f"Une personne pilote son navigateur à la voix et dit : « {request} ».\n"
                                              f"Voici les éléments de la page qui pourraient correspondre, en gros plan :"}]
    for e in candidates:
        content += [{"type": "input_text", "text": f"Candidat {describe(e)} :"},
                    {"type": "input_image", "image_url": jpeg_data_url(close_up(full, e))}]
    choices = [{"value": str(e["n"]), "description": describe(e)} for e in candidates]
    choices.append({"value": "none", "description": "Aucun de ces éléments ne correspond à sa demande"})

    decision = client.decisions.create(
        model=MODEL, input=[{"role": "user", "content": content}],
        questions=[{"type": "choice", "name": "target", "choices": choices, "instructions": QUESTION[d["action"]]}],
    )
    answer = decision.answers[0]
    shortlist = [str(e["n"]) for e in candidates]
    if answer.type != "choice" or answer.choice == "none" or answer.confidence < CONFIRMED:
        return {**d, "target": None, "target_confidence": answer.confidence if answer.type == "choice" else 0.0,
                "shortlist": shortlist}
    return {**d, "target": answer.choice, "target_confidence": answer.confidence, "shortlist": shortlist}


def close_up(full, e):
    """Le gros plan d'un élément, découpé dans la capture (avec une petite marge autour)."""
    box = (max(0, e["x"] - MARGIN), max(0, e["y"] - MARGIN),
           min(full.width, e["x"] + e["w"] + MARGIN), min(full.height, e["y"] + e["h"] + MARGIN))
    return full.crop(box)


def jpeg_data_url(image):
    out = io.BytesIO()
    image.convert("RGB").save(out, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()
