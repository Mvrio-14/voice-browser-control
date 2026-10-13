# voice-browser-control

Piloter son navigateur à la voix, en temps réel : « cherche lofi girl », « clique sur la vidéo avec le chat », « descends jusqu'à voir une vidéo de Squeezie », « mets la vidéo à la moitié », « ouvre wikipedia dans un nouvel onglet »...

Prototype volontairement simple et « académique » : chaque modèle fait une seule chose, le code fait le reste.

## Comment ça marche

```
micro ──► « hey browser » (Vosk, local) ──► session : Silero VAD (local) ──► AssemblyAI (streaming + fin de phrase) ou Gradium
                    └─ empreinte de la voix du réveil (ECAPA, local) : on n'écoute qu'elle
                                         │ texte
                                         ▼
page ──► see.py : capture d'écran + éléments cliquables numérotés (Set-of-Marks)
                                         │
                                         ▼
          decide.py : OpenAI Decisions (texte + image) → action, cible, texte, quantité, durée, onglet
                                         │   (écrire dans un champ : edit.py, gpt-5.4-mini → nouveau texte)
                                         │
                                         ▼
          act.py / motion.py : Playwright exécute (clic, recherche, scroll, zoom, vidéo, onglets...)
```

| Fichier | Rôle |
| --- | --- |
| `voice_click/main.py` | Boucle principale : veille → « hey browser » → session (écoute → voir → décider → agir) |
| `voice_click/listen.py` | Micro, mot de réveil (Vosk), détection de voix (Silero) et transcription (AssemblyAI ou Gradium) |
| `voice_click/speaker.py` | Empreinte vocale : ne suivre que la voix qui a dit « hey browser » (désactivé : `FOLLOW_VOICE` dans main.py) |
| `voice_click/see.py` | Ce que voit le modèle : capture, éléments numérotés et décrits, état de la page |
| `voice_click/decide.py` | Les questions posées au modèle de décision |
| `voice_click/act.py` | Exécution des actions ponctuelles |
| `voice_click/edit.py` | Écrire dans un champ : un petit LLM calcule le nouveau texte, le code tape la différence comme un humain |
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

Copie `.env.example` en `.env` et mets-y tes clés `ASSEMBLYAI_API_KEY` et `OPENAI_API_KEY` (et `GRADIUM_API_KEY` pour `--gradium`). Le fichier `.env` n'est jamais versionné.

## Lancer

```bash
python voice_click/main.py
```

Dis **« hey browser »** (ou appuie sur **Ctrl+Maj+Espace**) : une session s'ouvre, et tu enchaînes les commandes sans rien toucher. Après 5 s sans que tu parles (les autres voix ne comptent pas), ou un nouvel appui sur le raccourci, l'outil repasse en veille. Tu peux aussi taper une commande dans le panneau en bas de la page.

Au premier lancement, deux petits modèles locaux sont téléchargés : Vosk (40 Mo, mot de réveil) et ECAPA (80 Mo, empreinte vocale).

Options :
- `--type` : saisie au clavier dans le terminal, sans micro ;
- `--playwright` : utilise le Chromium de Playwright au lieu de Google Chrome ;
- `--no-image` : le modèle décide sur la liste des éléments seule, sans capture d'écran (plus rapide, mais les demandes visuelles échouent) ;
- `--gradium` : transcription par Gradium au lieu d'AssemblyAI. Il faut alors `GRADIUM_API_KEY` dans `.env`.

Au premier lancement, un Chrome avec un profil dédié « voice_click » s'ouvre. Connecte-toi une fois à tes sites dedans : c'est conservé.
