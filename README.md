# voice-browser-control

Piloter son navigateur à la voix, en temps réel : « cherche lofi girl », « clique sur la vidéo avec le chat », « descends jusqu'à voir une vidéo de Squeezie », « mets la vidéo à la moitié », « ouvre wikipedia dans un nouvel onglet »...

Prototype volontairement simple et « académique » : chaque modèle fait une seule chose, le code fait le reste.

## Comment ça marche

```
micro ──► Silero VAD (local) ──► Gradium STT (streaming + fin de tour sémantique)
                                         │ texte
                                         ▼
page ──► see.py : capture d'écran + éléments cliquables numérotés (Set-of-Marks)
                                         │
                                         ▼
          decide.py : OpenAI Decisions (texte + image) → action, cible, texte, quantité, durée, onglet
                                         │
                                         ▼
          act.py / motion.py : Playwright exécute (clic, recherche, scroll, zoom, vidéo, onglets...)
```

| Fichier | Rôle |
| --- | --- |
| `voice_click/main.py` | Boucle principale : raccourci clavier → écoute → voir → décider → agir |
| `voice_click/listen.py` | Micro, détection de voix (Silero) et transcription (Gradium) |
| `voice_click/see.py` | Ce que voit le modèle : capture, éléments numérotés et décrits, état de la page |
| `voice_click/decide.py` | Les questions posées au modèle de décision |
| `voice_click/act.py` | Exécution des actions ponctuelles |
| `voice_click/motion.py` | Mouvements continus (« jusqu'à ce que je dise stop », « jusqu'à voir X ») |
| `voice_click/panel.py` | Panneau flottant dans la page (statut, niveau du micro, saisie texte) |
| `voice_click/browser.py` | Connexion à ton vrai Google Chrome (profil dédié) |
| `voice_click/resolve.py` | Variante expérimentale : choix de la cible en deux temps (non branchée) |
| `voice_click/eval/` | Banc d'essai : situations figées et cas de test pour comparer les méthodes |

## Installation

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

Copie `.env.example` en `.env` et mets-y tes clés `OPENAI_API_KEY` et `GRADIUM_API_KEY`. Le fichier `.env` n'est jamais versionné.

## Lancer

```bash
python voice_click/main.py
```

Appuie sur **Ctrl+Maj+Espace** puis parle. Tu peux aussi taper une commande dans le panneau en bas de la page.

Options :
- `--type` : saisie au clavier dans le terminal, sans micro ;
- `--playwright` : utilise le Chromium de Playwright au lieu de Google Chrome.

Au premier lancement, un Chrome avec un profil dédié « voice_click » s'ouvre. Connecte-toi une fois à tes sites dedans : c'est conservé.
