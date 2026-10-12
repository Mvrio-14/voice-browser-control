"""OREILLES : micro → Silero (quelqu'un parle ?) → Gradium (texte + fin de tour).

1. Silero tourne en local sur le processeur. Il écoute des morceaux de 32 ms et répond
   « probabilité qu'il y ait une voix ». Tant que personne ne parle, rien n'est envoyé à Gradium.
2. Dès qu'il entend une voix, on ouvre Gradium et on lui envoie l'audio en direct.
   Gradium renvoie le texte, et toutes les 80 ms un message `step` avec la probabilité
   que l'utilisateur ne parle plus dans les X prochaines secondes : c'est la fin du tour.
"""
import asyncio
import contextlib
import warnings
from collections import deque

import numpy as np
import sounddevice as sd
import torch
from gradium.client import GradiumClient
from silero_vad import load_silero_vad

SAMPLE_RATE = 16000  # Silero et Gradium acceptent tous les deux du 16 kHz
CHUNK = 512  # 512 échantillons = 32 ms : la taille qu'attend Silero en 16 kHz
VOICE_THRESHOLD = 0.5  # Silero : au-dessus, on considère que quelqu'un parle
END_HORIZON = 1.0  # Gradium : « ne parlera plus dans la prochaine seconde »...
END_THRESHOLD = 0.8  # ... avec une probabilité au-dessus de ce seuil = fin du tour
NO_WORDS_S = 1.5  # si, passé ce délai, la voix s'est tue sans qu'aucun mot soit compris (toux, bruit) : on arrête
MAX_TURN_S = 20  # sécurité : un tour de parole ne dure jamais plus de 20 s
WAIT_VOICE_S = 6  # après le raccourci, on abandonne si personne ne parle pendant 6 s
DELAY = 10  # Gradium attend DELAY × 80 ms d'audio avant d'écrire un mot : plus = plus de contexte, moins = plus réactif

warnings.filterwarnings("ignore", category=FutureWarning)  # avertissement interne de PyTorch, sans conséquence
silero = load_silero_vad()


async def listen(on_voice=None, on_text=None, on_level=None):
    """Écoute UNE demande et renvoie son texte ("" si personne n'a parlé).
    Fonctions appelées au fil de l'écoute (toutes facultatives) :
      on_voice : dès que Silero entend une voix, AVANT la transcription
                 (ex : arrêter le défilement immédiatement, sans attendre la fin de la phrase)
      on_text  : à chaque nouveau mot transcrit, avec le texte jusqu'ici (ex : l'afficher en direct)
      on_level : ~12 fois par seconde, avec le niveau sonore du micro entre 0 et 1 (ex : animer une onde)"""
    loop = asyncio.get_running_loop()
    mic = asyncio.Queue()
    level = 0.0

    def on_audio(indata, frames, time, status):  # appelé par sounddevice dans un autre thread
        nonlocal level
        level = min(1.0, float(np.abs(indata).mean()) / 2000)  # volume moyen du morceau, ramené entre 0 et 1
        loop.call_soon_threadsafe(mic.put_nowait, indata[:, 0].copy())

    async def report_level():
        while True:
            await on_level(level)
            await asyncio.sleep(0.08)

    reporter = asyncio.create_task(report_level()) if on_level else None
    try:
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=CHUNK, callback=on_audio):
            print("👂 Je t'écoute...")
            start = await wait_for_voice(mic)
            if start is None:
                print("🤐 Personne n'a parlé.")
                return ""
            if on_voice:
                await on_voice()
            try:
                return await asyncio.wait_for(transcribe(mic, start, on_text), MAX_TURN_S)
            except TimeoutError:
                print("⏰ Tour de parole trop long : j'arrête d'écouter.")
                return ""
    finally:
        if reporter:
            reporter.cancel()


async def wait_for_voice(mic):
    """Étape 1 (local, gratuit) : lit le micro jusqu'à ce que Silero entende une voix."""
    silero.reset_states()
    recent = deque(maxlen=10)  # garde les 320 dernières ms pour ne pas couper le début du premier mot
    for _ in range(int(WAIT_VOICE_S * SAMPLE_RATE / CHUNK)):
        chunk = await mic.get()
        recent.append(chunk)
        voice_prob = silero(torch.from_numpy(chunk.astype(np.float32) / 32768), SAMPLE_RATE).item()
        if voice_prob > VOICE_THRESHOLD:
            return list(recent)
    return None


async def transcribe(mic, start, on_text=None):
    """Étape 2 (Gradium) : envoie l'audio en direct jusqu'à ce que Gradium juge le tour terminé."""
    words = []
    flushing = False
    async with GradiumClient().stt_realtime(
        model_name="default", input_format="pcm_16000", json_config={"language": "fr", "delay_in_frames": DELAY}
    ) as stt:
        for chunk in start:  # d'abord les 320 ms d'avant la détection
            await stt.send_audio(chunk)

        async def send_mic():  # puis le micro en continu, en parallèle de la lecture des messages
            with contextlib.suppress(Exception):  # (la connexion se ferme à la fin du tour : normal)
                while True:
                    await stt.send_audio(await mic.get())

        sender = asyncio.create_task(send_mic())

        async for msg in stt:
            if msg["type"] == "text":
                words.append(msg["text"])
                print("   ...", " ".join(words))
                if on_text:
                    await on_text(" ".join(words))

            elif msg["type"] == "step" and not flushing:
                end_prob = {v["horizon_s"]: v["inactivity_prob"] for v in msg["vad"]}.get(END_HORIZON, 0)
                # Fin du tour : la voix s'est tue... après des mots, ou sans qu'aucun mot n'ait été compris.
                if end_prob > END_THRESHOLD and (words or msg.get("total_duration_s", 0) > NO_WORDS_S):
                    # On demande à Gradium de nous envoyer tout le texte encore en attente.
                    await stt.send_flush()
                    flushing = True

            elif msg["type"] == "flushed":  # tout le texte est arrivé
                break

        sender.cancel()
    return " ".join(words)
