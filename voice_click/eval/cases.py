"""ÉVALUATION : les demandes à tester sur chaque situation enregistrée, avec la bonne cible.

La bonne cible est le numéro de l'élément sur la capture de la situation (eval/fixtures/<situation>.jpg),
vérifié à l'œil. None = l'élément demandé n'est PAS à l'écran : la bonne réponse est de ne rien faire.
Un ensemble {..} = plusieurs réponses acceptables (ex : élément à peine visible, coupé par le bord).
"""

CASES = [
    # (situation, demande, action attendue, cible attendue)

    # YouTube « GreenCode », la vidéo au chat bien visible (n°25), le piège « LLM from Scratch » coupé en haut
    ("yt_greencode_chat", "clique sur la vidéo avec le chat", "click", "25"),
    ("yt_greencode_chat", "clique sur la vidéo avec le chat orange", "click", "25"),
    ("yt_greencode_chat", "clique sur la vidéo avec le réseau de neurones mais sans le chat", "click", "30"),
    ("yt_greencode_chat", "clique sur la chaîne Green Code", "click", "27"),
    ("yt_greencode_chat", "cherche lofi girl", "search", "2"),
    ("yt_greencode_chat", "clique sur la vidéo avec un dinosaure", "click", None),

    # Même page, plus haut : deux vidéos « LLM from Scratch » ; la vidéo au chat est à peine visible en bas
    ("yt_greencode_llm", "clique sur la vidéo avec le chat", "click", {None, "32"}),
    ("yt_greencode_llm", "clique sur la vidéo avec le logo OpenAI rouge", "click", "21"),
    ("yt_greencode_llm", "clique sur la vidéo où il y a un chiffre 1 en bas à gauche", "click", "26"),
    ("yt_greencode_llm", "écris bonjour dans la barre de recherche", "type_text", "2"),
    ("yt_greencode_llm", "clique sur la vidéo avec un dinosaure", "click", None),

    # Même page, tout en haut : la chaîne, puis les vidéos LLM ; pas de chat
    ("yt_greencode_haut", "clique sur la vidéo avec le logo OpenAI rouge", "click", "31"),
    ("yt_greencode_haut", "clique sur l'avatar rond de la chaîne", "click", "29"),
    ("yt_greencode_haut", "clique sur s'abonner", "click", "30"),
    ("yt_greencode_haut", "clique sur l'onglet Shorts", "click", "22"),
    ("yt_greencode_haut", "clique sur la vidéo avec le chat", "click", None),

    # YouTube « animaux drôles » : éléphants (pub), poney, husky (le chat de la 3e est caché par la barre)
    ("yt_animaux", "clique sur la vidéo avec les éléphants", "click", "29"),
    ("yt_animaux", "clique sur la vidéo avec le poney", "click", "33"),
    ("yt_animaux", "clique sur la vidéo avec le husky", "click", "38"),
    ("yt_animaux", "clique sur la vidéo avec un chat", "click", {None, "38"}),
    ("yt_animaux", "clique sur la vidéo avec un pingouin", "click", None),

    # Wikipédia, Alan Turing, milieu de page : liens de texte, signature, bandeau de dons
    ("wiki_turing_milieu", "clique sur le lien Machine de Turing", "click", "34"),
    ("wiki_turing_milieu", "clique sur la signature", "click", "42"),
    ("wiki_turing_milieu", "ferme le bandeau de dons", "click", "1"),
    ("wiki_turing_milieu", "clique sur Marlborough College", "click", "57"),
    ("wiki_turing_milieu", "clique sur le lien Indian", "click", "54"),
    ("wiki_turing_milieu", "clique sur le lien vers Napoléon", "click", None),

    # Wikipédia, haut de page : champs
    ("wiki_turing_haut", "cherche Enigma", "search", "3"),
    ("wiki_turing_haut", "écris 10 dans le champ Autre", "type_text", "20"),
]
