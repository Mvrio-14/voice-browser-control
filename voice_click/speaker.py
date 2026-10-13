"""VOIX : ne suivre que la personne qui a dit « hey browser » (utile dans un café).

Au réveil, on retient deux choses de la phrase « hey browser » :
  - son TIMBRE : une « empreinte vocale », un vecteur de 192 nombres calculé par ECAPA (modèle de
    SpeechBrain entraîné à reconnaître les locuteurs, en local) ;
  - son VOLUME : la personne qui parle au micro est plus près que les voix du café, donc plus forte.
Ensuite, chaque extrait reçoit un score : la ressemblance de timbre (de -1 à 1), moins un petit malus
si la voix est nettement plus faible que le réveil (une voix lointaine) : -0,01 par décibel au-delà de
6 dB d'écart, plafonné à -0,1. Pas de bonus pour une voix plus forte (un voisin qui parle fort ne doit pas
être favorisé), ni de malus pour les petits écarts (ta propre voix varie de quelques décibels).
Au-dessus de SAME_VOICE, c'est la même voix.

Pas de configuration : tout vient du seul « hey browser » (moins d'une seconde de voix), donc c'est
approximatif. Sur 6 voix de synthèse (timbre seul) : ~93 % de tes phrases gardées, ~80 % des autres écartées.

Le même score sert pendant l'écoute (listen.py) : toutes les 0,5 s, on regarde qui parle dans la dernière
1,5 s, pour couper la phrase quand la voix change (ta commande ne se mélange plus à la voisine).
C'est une version simplifiée de la « diarisation en direct » (diart, pyannote) et de la
« détection de voix personnalisée » (Personal VAD de Google), avec un seul locuteur à suivre.
"""
import os
import warnings

import numpy as np
import torch
from speechbrain.inference.speaker import EncoderClassifier
from speechbrain.utils.fetching import LocalStrategy

SAME_VOICE = 0.2  # score en dessous duquel on considère que c'est une autre voix
QUIETER_DB = 6  # jusqu'à 6 dB plus faible que le réveil : c'est normal, pas de malus
DB_WEIGHT = 0.01  # au-delà : malus de score par décibel
MAX_DB_MALUS = 0.1  # le volume aide à départager, mais ne décide jamais seul

warnings.filterwarnings("ignore", message=".*symlink.*")
ecapa = EncoderClassifier.from_hparams(
    source="speechbrain/spkrec-ecapa-voxceleb",  # (80 Mo, téléchargé au premier lancement)
    savedir=os.path.join(os.environ["LOCALAPPDATA"], "voice_click", "models", "ecapa"),
    local_strategy=LocalStrategy.COPY,  # (Windows n'autorise pas les liens symboliques sans mode développeur)
)


def voiceprint(audio):
    """L'empreinte vocale d'un extrait audio (16 kHz, int16) : un vecteur de longueur 1."""
    with torch.no_grad():
        e = ecapa.encode_batch(torch.from_numpy(audio.astype(np.float32) / 32768)[None]).squeeze()
    return (e / e.norm()).numpy()


def loudness(audio):
    """Le volume de la voix, en décibels : on prend les passages les plus forts (les silences ne comptent pas)."""
    frames = audio[: len(audio) // 512 * 512].astype(np.float32).reshape(-1, 512)
    rms = np.sqrt((frames ** 2).mean(axis=1)) + 1
    return float(20 * np.log10(np.percentile(rms, 90)))


class Voice:
    """La voix à suivre, retenue au réveil."""

    def __init__(self, wake_audio):
        self.print = voiceprint(wake_audio)
        self.db = loudness(wake_audio)

    def score(self, audio):
        """Renvoie (score, ressemblance de timbre, écart de volume en dB par rapport au réveil)."""
        resemblance = float(self.print @ voiceprint(audio))
        db = loudness(audio) - self.db
        malus = min(MAX_DB_MALUS, max(0.0, -db - QUIETER_DB) * DB_WEIGHT)
        return resemblance - malus, resemblance, db

    def is_mine(self, audio):
        return self.score(audio)[0] >= SAME_VOICE
