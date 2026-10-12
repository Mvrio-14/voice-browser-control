"""CERVEAU : OpenAI Decisions répond à des questions en choisissant parmi les réponses qu'on lui propose.

Le modèle ne génère pas de texte. On lui donne :
  - input     : ce qu'il regarde (le contexte, les demandes précédentes, la nouvelle demande, la capture numérotée)
  - questions : 5 questions, posées dans UNE seule requête :
      1. action   (choice) : QUOI faire ? (cliquer, chercher, défiler, zoomer, avancer la vidéo...)
      2. target   (choice) : SUR QUEL élément numéroté ? (celui à cliquer, ou le champ dans lequel écrire)
                             (seulement s'il y a des éléments à l'écran : l'API exige au moins 2 choix)
      3. text     (choice) : quel passage de sa phrase utiliser (texte à écrire / chercher, adresse à ouvrir,
                             moment de la vidéo, ou condition d'arrêt)
      4. amount   (score)  : avec quelle ampleur (un peu / normal / beaucoup / au maximum)
      5. duration (choice) : une fois, en continu jusqu'à « stop », ou jusqu'à une condition (voir motion.py)
    Les questions 2 à 5 sont posées à chaque fois, même quand l'action n'en a pas besoin :
    ça ne coûte presque rien et ça évite un deuxième appel. Le code ignore les réponses inutiles.

Pourquoi séparer « quoi » (action) et « sur quoi » (target) ? Si chaque élément de la page était une
option de l'action, les ~60 éléments se partageraient un peu de probabilité chacun : sur une phrase
courte comme « encore », la bonne action passerait sous le seuil de confiance.

Côté formulation : on donne un contexte clair et des options bien distinctes, puis on fait confiance
au modèle, plutôt que d'empiler des règles.
"""
import base64

from openai import OpenAI

MODEL = "gpt-6-luna"
client = OpenAI()  # créé une seule fois : la connexion est réutilisée d'un appel à l'autre (plus rapide)

CONTEXT = (
    "Une personne pilote son navigateur web à la voix, comme elle le ferait avec une souris et un clavier. "
    "La capture d'écran montre la page qu'elle a sous les yeux ; les éléments cliquables y sont encadrés "
    "en rouge et numérotés."
)

ACTIONS = [
    # Chaque option décrit un résultat DIFFÉRENT, avec des exemples de phrases : deux options qui se
    # recouvrent (ex : chercher = écrire + valider) se partageraient la probabilité, et la confiance chuterait.
    {"value": "click", "description": "Cliquer sur un élément de la page (lien, bouton, vidéo, image...)"},
    {"value": "search", "description": "Faire une recherche complète SUR LE SITE affiché : écrire dans sa barre de "
                                       "recherche puis lancer (« cherche X », « recherche X »)"},
    {"value": "web_search", "description": "Faire une recherche SUR LE WEB, avec un moteur de recherche "
                                           "(« recherche sur le web X », « google X », « fais une recherche internet »)"},
    {"value": "type_text", "description": "Seulement écrire du texte dans un champ, sans valider "
                                          "(« écris X », « tape X », remplir un formulaire)"},
    {"value": "press_enter", "description": "Appuyer sur Entrée, valider (« valide », « entrée », « envoie »)"},
    {"value": "open_url", "description": "Ouvrir un site dont la personne a donné l'adresse, DANS L'ONGLET ACTUEL "
                                         "(« va sur youtube.com »)"},
    {"value": "scroll_down", "description": "Descendre dans la PAGE (« descends », « scroll »)"},
    {"value": "scroll_up", "description": "Remonter dans la PAGE (« remonte »)"},
    {"value": "stop", "description": "Arrêter le mouvement en cours (« stop », « arrête »)"},
    {"value": "zoom_in", "description": "Zoomer, agrandir l'affichage"},
    {"value": "zoom_out", "description": "Dézoomer, réduire l'affichage"},
    {"value": "zoom_reset", "description": "Revenir à la taille d'affichage normale"},
    {"value": "back", "description": "Revenir à la page précédente"},
    {"value": "reload", "description": "Recharger / rafraîchir la page (« refresh », « recharge la page »)"},
    {"value": "tab_new", "description": "Ouvrir un NOUVEL onglet, vide ou sur une adresse donnée "
                                        "(« ouvre un nouvel onglet », « ouvre youtube.com dans un nouvel onglet »)"},
    {"value": "tab_switch", "description": "Aller sur un autre onglet déjà ouvert (« va sur l'onglet YouTube », "
                                           "« onglet suivant », « reviens au premier onglet »)"},
    {"value": "tab_close", "description": "Fermer un onglet (« ferme cet onglet », « ferme l'onglet Wikipédia »)"},
    {"value": "video_play", "description": "Lancer ou reprendre la lecture de la vidéo"},
    {"value": "video_pause", "description": "Mettre la vidéo en pause"},
    {"value": "video_mute", "description": "Couper le son de la vidéo"},
    {"value": "video_unmute", "description": "Remettre le son de la vidéo"},
    {"value": "video_volume_up", "description": "Monter le volume de la vidéo (« augmente le son », « plus fort »)"},
    {"value": "video_volume_down", "description": "Baisser le volume de la vidéo (« baisse le son », « moins fort »)"},
    {"value": "video_forward", "description": "Avancer dans la VIDÉO (« avance », « avance de 30 secondes », "
                                              "« avance jusqu'à ce que je dise stop »)"},
    {"value": "video_back", "description": "Reculer dans la VIDÉO (« recule », « recule de 10 secondes », "
                                           "« recule jusqu'à ce qu'on voie... »)"},
    {"value": "video_goto", "description": "Aller directement à un moment précis de la vidéo (« va à la minute 12 », "
                                           "« mets à 3:30 », « va à la moitié », « avance à 80 % », « aux trois quarts »)"},
    {"value": "none", "description": "Rien à faire : ce n'est pas une demande, ou aucune option ne convient"},
]

# Combien de temps dure le mouvement (défiler, avancer / reculer la vidéo, zoomer) ?
DURATIONS = [
    {"value": "once", "description": "Une seule fois, d'une quantité donnée (« descends », « avance de 30 secondes », "
                                     "« zoome un peu », et aussi « jusqu'en bas », « au maximum »)"},
    {"value": "until_stop", "description": "En continu, sans s'arrêter, jusqu'à ce que la personne dise stop "
                                           "(« défile », « avance jusqu'à ce que je dise stop »)"},
    {"value": "until_condition", "description": "En continu, jusqu'à ce qu'une condition décrite par la personne soit "
                                                "remplie (« descends jusqu'à une vidéo de chat », « remonte jusqu'au "
                                                "titre », « recule jusqu'à ce qu'on voie la voiture rouge », "
                                                "« zoome jusqu'à ce que ce soit lisible »)"},
]

AMOUNT_LEVELS = [
    {"label": "un peu", "description": "Un petit peu, légèrement"},
    {"label": "normal", "description": "Rien de précisé"},
    {"label": "beaucoup", "description": "Beaucoup, franchement"},
    {"label": "au maximum", "description": "Jusqu'au bout : tout en bas, tout en haut, zoom ou dézoom maximal"},
]


def spans(text):
    """Tous les passages de mots consécutifs de la phrase : le modèle choisira le bon.
    "va sur youtube" → ["va", "va sur", "va sur youtube", "sur", "sur youtube", "youtube"]"""
    words = [w.strip(".,!?;:«»\"") for w in text.split()]
    words = [w for w in words if w]
    return [" ".join(words[i:j]) for i in range(len(words)) for j in range(i + 1, len(words) + 1)]


def describe(e):
    """Une ligne par élément (rôle, nom, état), préparée par see.py. Ex :
    « n°27 lien (miniature) « I Built a Neural Network from Scratch in SCRATCH » »
    « n°2 champ de recherche « Search » — contient « GreenCode » »"""
    line = f"n°{e['n']} {e['role']}" + (f" « {e['name']} »" if e["name"] else " (sans nom)")
    return line + (f" — {e['state']}" if e["state"] else "")


def describe_tab(t):
    """« onglet 2 « Alan Turing — Wikipédia » (fr.wikipedia.org), affiché » """
    return f"onglet {t['n']} « {t['title'] or 'nouvel onglet'} » ({t['site']})" + (", affiché" if t["active"] else "")


def decide(request, elements, screenshot, state, history=(), tabs=()):
    """state   : l'état actuel de la page en une phrase (page, zoom, position de défilement), voir see.py.
    tabs    : les onglets ouverts, ex : [{"n": 1, "title": "YouTube", "active": True}, ...] (voir browser.py).
    history : les dernières demandes et ce qui a été fait, ex : ["« dézoome » → 🔍 dézoomé à 80 % (normal)"].
    Le modèle n'a aucune mémoire entre deux appels : sans ça, « encore » ou « plus » ne veulent rien dire.
    Renvoie un dict, ex : {"action": "click", "confidence": 0.93, "target": "12", "target_confidence": 0.81,
                           "text": None, "amount": "normal", "duration": "once"}"""
    targets = [
        {"value": str(e["n"]), "description": describe(e)} for e in elements
    ] + [{"value": "none", "description": "Aucun élément de la page"}]
    texts = [{"value": s} for s in dict.fromkeys(spans(request))] + [{"value": "none"}]  # (fromkeys : sans doublons)

    # Dans l'ordre d'une conversation : la situation, ce qui s'est passé avant, puis ce qu'elle dit maintenant.
    context = CONTEXT + "\n\nÉtat actuel : " + state
    if tabs:
        context += "\nOnglets ouverts : " + " ; ".join(describe_tab(t) for t in tabs)
    if history:
        context += "\n\nCe qu'elle a demandé juste avant, et ce qui a été fait :\n" + "\n".join(history)
    context += f"\n\nElle dit maintenant : « {request} »"

    questions = [
        {"type": "choice", "name": "action", "choices": ACTIONS,
         "instructions": "Que veut faire la personne maintenant ? Sa phrase peut faire suite aux précédentes."},
        {"type": "choice", "name": "text", "choices": texts,
         "instructions": "Quel passage de sa phrase est le texte à écrire ou à chercher, l'adresse à ouvrir, "
                         "la durée / le moment de la vidéo (« 30 secondes », « 12 »), "
                         "ou la condition d'arrêt complète d'un mouvement (« une vidéo avec un chat sur la "
                         "miniature », « au tout début de la vidéo », « le texte soit bien lisible ») ? "
                         "Seulement ce passage, sans les mots de commande autour. Par exemple : "
                         "« cherche lofi girl » → « lofi girl » ; « google la météo à Paris » → « la météo à Paris » ; "
                         "« recherche sur le web les horaires SNCF » → « les horaires SNCF » ; "
                         "« va sur youtube.com » → « youtube.com »."},
        {"type": "score", "name": "amount", "levels": AMOUNT_LEVELS,
         "instructions": "Si elle veut zoomer, faire défiler la page ou avancer / reculer dans la vidéo, "
                         "de combien (ou à quelle vitesse) ?"},
        {"type": "choice", "name": "duration", "choices": DURATIONS,
         "instructions": "Si elle veut faire défiler la page, avancer / reculer dans la vidéo ou zoomer : "
                         "une fois, en continu jusqu'à « stop », ou jusqu'à ce que quelque chose se produise ?"},
    ]
    if tabs:
        tab_choices = [{"value": f"tab{t['n']}", "description": describe_tab(t)} for t in tabs] + [
            {"value": "next", "description": "L'onglet suivant"},
            {"value": "previous", "description": "L'onglet précédent"}]
        questions.append({"type": "choice", "name": "tab", "choices": tab_choices,
                          "instructions": "Si elle veut aller sur un autre onglet ou en fermer un : lequel ? "
                                          "(« ferme cet onglet » = l'onglet affiché)"})
    if elements:  # (page vide ou en chargement : pas d'élément, donc pas de question "sur quoi")
        questions.append({"type": "choice", "name": "target", "choices": targets,
                          "instructions": "Sur quel élément numéroté agir : celui sur lequel cliquer, "
                                          "ou le champ dans lequel écrire ou chercher ?"})

    image = "data:image/jpeg;base64," + base64.b64encode(screenshot).decode()
    decision = client.decisions.create(
        model=MODEL,
        input=[{"role": "user", "content": [
            {"type": "input_text", "text": context},
            {"type": "input_image", "image_url": image},
        ]}],
        questions=questions,
    )

    # On range les réponses par nom de question. Une réponse peut être un refus (type "refusal") : on l'ignore.
    answers = {a.name: a for a in decision.answers if a.type != "refusal"}
    action, target, text, amount, duration, tab = (
        answers.get(k) for k in ("action", "target", "text", "amount", "duration", "tab"))
    return {
        "action": action.choice if action else "none",
        "confidence": action.confidence if action else 0.0,
        "target": target.choice if target and target.choice != "none" else None,
        "target_confidence": target.confidence if target else 0.0,
        # toute la répartition (numéro → probabilité) : resolve.py y prend les meilleurs candidats
        "target_probabilities": {p.value: p.probability for p in target.probabilities} if target else {},
        "text": text.choice if text and text.choice != "none" else None,
        # le niveau le plus probable ("un peu", "normal" ou "beaucoup")
        "amount": max(amount.probabilities, key=lambda p: p.probability).label if amount else "normal",
        "duration": duration.choice if duration else "once",
        "tab": tab.choice if tab else None,  # "tab2", "next", "previous"...
        "request": request,  # la phrase complète, utile à motion.py pour juger une condition d'arrêt
    }


def condition_met(request, condition, screenshot, text, state):
    """Le 3e type de question : "predicate" (oui/non). Renvoie la probabilité que la condition d'arrêt du
    mouvement soit remplie (« descends jusqu'à une vidéo de chat » : voit-on une vidéo de chat ?).
    Utilisé en boucle par motion.py.
    - La phrase complète sert de CONTEXTE ; la condition extraite est l'AFFIRMATION à vérifier. Un modèle
      de décision juge bien mieux une affirmation concrète (« le texte est bien lisible ») qu'une question
      abstraite (« faut-il s'arrêter ? »). Sans condition extraite, on se rabat sur la question abstraite.
    - Il reçoit toutes les preuves, et choisit lui-même la bonne : l'image (« un chat sur la miniature »),
      le texte affiché (« une vidéo de Squeezie »), l'état (« la minute 5 », « le début de la vidéo »)."""
    image = "data:image/jpeg;base64," + base64.b64encode(screenshot).decode()
    if condition:
        question = f"Est-il vrai, maintenant, que : « {condition} » ?"
    else:
        question = "Ce qu'elle attendait est-il atteint maintenant, c'est-à-dire faut-il arrêter le mouvement ici ?"
    decision = client.decisions.create(
        model=MODEL,
        input=[{"role": "user", "content": [
            {"type": "input_text", "text": f"Une personne pilote son navigateur à la voix. Elle a demandé : "
                                           f"« {request} ». Le mouvement est en cours. Voici ce qui est "
                                           f"affiché à l'écran en ce moment.\n\n"
                                           f"État : {state}\n\nTexte visible : {text}"},
            {"type": "input_image", "image_url": image},
        ]}],
        questions=[{"type": "predicate", "name": "met", "instructions": question}],
    )
    answer = decision.answers[0]
    return answer.probability if answer.type == "predicate" else 0.0

