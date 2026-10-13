"""OREILLES : micro → « hey browser » (Vosk) → Silero (quelqu'un parle ?) → Gradium (texte + fin de tour).

Le micro reste ouvert pendant tout le programme, et garde la dernière minute d'audio (Microphone).
Chaque étape lit l'audio à son rythme, et peut revenir en arrière : quand une étape s'aperçoit un peu
tard que la phrase est finie, elle rend ce qu'elle a lu en trop. Rien ne se perd entre le mot de réveil et
la commande, ni entre deux commandes enchaînées. Tout est local et gratuit, sauf Gradium.

0. En veille, wait_for_wake() attend « hey browser ». Silero repère les moments où quelqu'un parle, et
   seulement ceux-là passent dans Vosk : un petit modèle de reconnaissance vocale anglais, en local.
   On attend le mot « browser » dans ce qu'il comprend. Avantage : les conversations autour de toi
   ne partent jamais sur Internet.
1. Silero tourne en local sur le processeur. Il écoute des morceaux de 32 ms et répond
   « probabilité qu'il y ait une voix ». Tant que personne ne parle, rien n'est envoyé à Gradium.
2. Dès qu'il entend une voix, on ouvre le service de transcription et on lui envoie l'audio en direct.
   Deux services au choix (ENGINE, ou --gradium au lancement). Tous deux décident eux-mêmes de la fin de la phrase :
   - Gradium : renvoie le texte, et toutes les 80 ms un message `step` avec la probabilité
     que l'utilisateur ne parle plus dans les X prochaines secondes : c'est la fin du tour.
     Si une voix est suivie (après « hey browser »), on peut aussi couper quand la voix change.
   - AssemblyAI (Universal-3.6 Pro, par défaut : plus rapide et plus juste à l'usage) : après un court silence, si le modèle juge la phrase complète
     (il a mis un point final), le tour est fini tout de suite ; sinon il attend, au plus 1 s de silence.
     UNE connexion pour toute la session « hey browser » (AssemblyAISession) : le micro y part en continu,
     et AssemblyAI découpe lui-même les phrases. C'est l'usage prévu par AssemblyAI (et le compte gratuit
     n'accepte que 5 nouvelles connexions par minute : une par phrase dépassait vite cette limite).
"""
import os
import asyncio
import contextlib
import json
import warnings
from urllib.parse import urlencode

import numpy as np
import sounddevice as sd
import torch
from gradium.client import GradiumClient
from silero_vad import load_silero_vad
from vosk import KaldiRecognizer, Model, SetLogLevel
from websockets.asyncio.client import connect

ENGINE = "assemblyai"  # le service de transcription : "assemblyai" ou "gradium" (main.py le change avec --gradium)
SAMPLE_RATE = 16000  # Silero, Vosk, Gradium et AssemblyAI acceptent tous du 16 kHz
CHUNK = 512  # 512 échantillons = 32 ms : la taille qu'attend Silero en 16 kHz
VOICE_THRESHOLD = 0.5  # Silero : au-dessus, on considère que quelqu'un parle
PREROLL = 10  # on garde les 10 morceaux (320 ms) d'avant la voix, pour ne pas couper le début du premier mot
GAP = 8  # 8 morceaux sans voix (256 ms) : la frontière entre deux phrases
END_HORIZON = 1.0  # Gradium : « ne parlera plus dans la prochaine seconde »...
END_THRESHOLD = 0.8  # ... avec une probabilité au-dessus de ce seuil = fin du tour
NO_WORDS_S = 1.5  # si, passé ce délai, la voix s'est tue sans qu'aucun mot soit compris (toux, bruit) : on arrête
MAX_TURN_S = 20  # sécurité : un tour de parole ne dure jamais plus de 20 s
DELAY = 10  # Gradium attend DELAY × 80 ms d'audio avant d'écrire un mot : plus = plus de contexte, moins = plus réactif
WAKE_WORD = "browser"  # Vosk (anglais) entend « hey browser » ; en français, « Bruno » ou « bonjour » ne donnent jamais ce mot
WAKE_PAUSE_S = 1.0  # après 1 s sans voix, la phrase est finie : Vosk repart de zéro pour la suivante
WINDOW_S = 1.5  # pendant l'écoute, on regarde QUI parle dans la dernière 1,5 s...
STEP_S = 0.5  # ... toutes les 0,5 s ; la phrase est coupée si 2 fenêtres de suite désignent une autre voix
KEEP_S = 60  # le micro garde la dernière minute d'audio
ASSEMBLYAI_URL = "wss://streaming.assemblyai.com/v3/ws"
ASSEMBLYAI_MODEL = "universal-3-6-pro"  # parle français, et change de langue tout seul (pas de paramètre de langue)

warnings.filterwarnings("ignore", category=FutureWarning)  # avertissement interne de PyTorch, sans conséquence
silero = load_silero_vad()
SetLogLevel(-1)  # Vosk est très bavard dans la console
vosk = Model(model_name="vosk-model-small-en-us-0.15")  # (40 Mo, téléchargé au premier lancement)


def seconds(chunks):
    return chunks * CHUNK / SAMPLE_RATE


def chunks_in(s):
    return int(s * SAMPLE_RATE / CHUNK)


class Microphone:
    """Le micro, ouvert une fois pour toutes. Il garde l'audio reçu, et un curseur de lecture :
    get() lit le morceau de 32 ms suivant (en attendant qu'il arrive), rewind() revient en arrière.
    Les positions sont des numéros de morceaux depuis l'ouverture. `level` : le volume du moment, de 0 à 1."""

    def __init__(self):
        self.audio = []  # les morceaux gardés ; audio[0] est le morceau n° `base`
        self.base = 0
        self.position = 0  # le prochain morceau à lire
        self.arrived = asyncio.Event()
        self.level = 0.0
        self.stream = None

    def add(self, chunk):
        self.audio.append(chunk)
        self.arrived.set()
        old = self.position - self.base - chunks_in(KEEP_S)
        if old > chunks_in(KEEP_S):  # (de temps en temps, on oublie l'audio de plus d'une minute déjà lu)
            del self.audio[:old]
            self.base += old

    async def get(self):
        while self.position >= self.base + len(self.audio):
            self.arrived.clear()
            await self.arrived.wait()
        self.position += 1
        return self.audio[self.position - 1 - self.base]

    async def at(self, position):
        """Le morceau n° `position` (en attendant qu'il arrive), sans bouger le curseur : pour un second lecteur
        (ex : la connexion AssemblyAI, qui reçoit tout le micro pendant la session)."""
        while position >= self.base + len(self.audio):
            self.arrived.clear()
            await self.arrived.wait()
        return self.audio[max(position, self.base) - self.base]

    def rewind(self, position):
        self.position = max(position, self.base)

    def between(self, start, end):
        """L'audio entre deux positions, en un seul tableau."""
        return np.concatenate(self.audio[max(start, self.base) - self.base:end - self.base])

    def __enter__(self):
        loop = asyncio.get_running_loop()

        def on_audio(indata, frames, time, status):  # appelé par sounddevice dans un autre thread
            self.level = min(1.0, float(np.abs(indata).mean()) / 2000)
            loop.call_soon_threadsafe(self.add, indata[:, 0].copy())

        self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=CHUNK,
                                     callback=on_audio)
        self.stream.start()
        return self

    def __exit__(self, *exc):
        self.stream.close()


def is_voice(chunk):
    return silero(torch.from_numpy(chunk.astype(np.float32) / 32768), SAMPLE_RATE).item() > VOICE_THRESHOLD


async def wait_for_wake(mic):
    """Étape 0 (local, gratuit) : attend « hey browser ». Renvoie l'audio de « hey browser » (de quoi
    reconnaître ensuite la voix de la personne qui l'a dit), et laisse le micro juste après : si la commande
    suit d'une traite (« hey browser, cherche lofi girl »), elle sera lue ensuite."""
    silero.reset_states()
    start = None  # le début de la phrase en cours (None = silence)
    while True:
        chunk = await mic.get()
        voice = is_voice(chunk)
        if start is None:
            if not voice:
                continue
            start = max(mic.position - 1 - PREROLL, mic.base)
            silence = 0
            recognizer = KaldiRecognizer(vosk, SAMPLE_RATE)
            recognizer.SetWords(True)  # (pour savoir quand « browser » se termine)
            recognizer.AcceptWaveform(mic.between(start, mic.position).tobytes())
            continue

        if recognizer.AcceptWaveform(chunk.tobytes()):
            result = json.loads(recognizer.Result())
            heard = result["text"]
        else:
            result = None
            heard = json.loads(recognizer.PartialResult())["partial"]
        if WAKE_WORD in heard.split():
            words = (result or json.loads(recognizer.FinalResult())).get("result", [])
            end_s = next((w["end"] for w in words if w["word"] == WAKE_WORD), None)
            end = start + chunks_in(end_s) + 1 if end_s else mic.position
            mic.rewind(min(end, mic.position))
            return mic.between(start, mic.position)

        silence = 0 if voice else silence + 1
        if seconds(silence) > WAKE_PAUSE_S:
            start = None


async def listen(mic, wait_s, on_voice=None, on_text=None, voice=None, stream=None):
    """Écoute UNE demande. Renvoie (texte, audio) :
      - (None, None) si personne n'a parlé pendant wait_s secondes ;
      - ("", audio) si une voix a été entendue sans qu'aucun mot soit compris (toux, bruit).
    Fonctions appelées au fil de l'écoute (facultatives) :
      on_voice : dès que Silero entend une voix, AVANT la transcription
                 (ex : arrêter le défilement immédiatement, sans attendre la fin de la phrase)
      on_text  : à chaque nouveau mot transcrit, avec le texte jusqu'ici (ex : l'afficher en direct)
    voice : la voix suivie (speaker.Voice). Si elle est donnée, la demande s'arrête quand la voix change :
      ta phrase et celle du voisin deviennent deux demandes, au lieu d'une seule mélangée.
    stream : la connexion AssemblyAI de la session (AssemblyAISession), si ENGINE vaut "assemblyai".
      Sans elle (ex : écoute pendant un défilement, hors session), une connexion est ouverte pour cette seule demande."""
    if ENGINE == "assemblyai" and stream:
        # Une phrase a pu être terminée pendant que la commande précédente s'exécutait : on la prend tout de suite.
        begin = mic.position
        heard = asyncio.create_task(wait_for_voice(mic, wait_s))
        turn = asyncio.create_task(stream.turns.get())
        await asyncio.wait({heard, turn}, return_when=asyncio.FIRST_COMPLETED)
        if turn.done():
            heard.cancel()
            text = turn.result()
            if isinstance(text, Exception):
                raise text
            mic.rewind(mic.base + len(mic.audio))  # (tout ce qui précède a été traité par AssemblyAI)
            return text, mic.between(begin, mic.position)
        turn.cancel()
        start = heard.result()
    else:
        start = await wait_for_voice(mic, wait_s)
    if start is None:
        return None, None
    if on_voice:
        await on_voice()
    try:
        if ENGINE == "assemblyai":
            text, end = await asyncio.wait_for(next_turn(mic, start, on_text, stream), MAX_TURN_S)
        else:
            text, end = await asyncio.wait_for(transcribe(mic, start, on_text, voice), MAX_TURN_S)
    except TimeoutError:
        print("⏰ Tour de parole trop long : j'arrête d'écouter.")
        text, end = "", mic.position
    mic.rewind(end)  # ce qui a été lu après la fin de la demande appartient à la suivante
    return text, mic.between(start, end)


async def wait_for_voice(mic, wait_s):
    """Étape 1 (local, gratuit) : lit le micro jusqu'à ce que Silero entende une voix.
    Renvoie la position du début de la voix (avec un peu de marge), ou None."""
    silero.reset_states()
    for _ in range(chunks_in(wait_s)):
        if is_voice(await mic.get()):
            return max(mic.position - 1 - PREROLL, mic.base)
    return None


def next_phrase(voiced, after):
    """Où commence une nouvelle phrase, après une pause d'au moins GAP morceaux, dans `voiced`
    (une liste de booléens : voix ou pas, morceau par morceau) ? Renvoie son indice, ou None."""
    quiet = 0
    found = None
    for i, v in enumerate(voiced):
        if v and quiet >= GAP and i > after:
            found = i
        quiet = 0 if v else quiet + 1
    return found


async def transcribe(mic, start, on_text=None, voice=None):
    """Étape 2 (Gradium) : envoie l'audio en direct jusqu'à ce que Gradium juge le tour terminé,
    ou jusqu'à ce que la voix change. Renvoie (texte, position de la fin de la demande)."""
    words = []  # [(instant en secondes depuis `start`, texte)]
    voiced = []  # pour chaque morceau envoyé après le début : voix ou pas (Silero)
    cut = None  # position où la demande s'arrête, si la voix a changé
    flushing = False
    step, window = chunks_in(STEP_S), chunks_in(WINDOW_S)
    first = mic.position  # (les morceaux d'avant ont été lus par wait_for_voice)

    def said(limit=None):
        return " ".join(w for t, w in words if limit is None or t < seconds(limit - start) - 0.05)

    async with GradiumClient().stt_realtime(
        model_name="default", input_format="pcm_16000", json_config={"language": "fr", "delay_in_frames": DELAY}
    ) as stt:
        await stt.send_audio(mic.between(start, first))  # d'abord la marge d'avant la voix

        async def end_turn():
            nonlocal flushing
            if flushing:  # (la voix a changé ET Gradium a jugé la phrase finie au même moment)
                return
            # On demande à Gradium de nous envoyer tout le texte encore en attente, et on arrête de lire le micro.
            sender.cancel()
            await stt.send_flush()
            flushing = True

        async def send_mic():  # puis le micro en continu, en parallèle de la lecture des messages
            nonlocal cut
            owners = []  # pour chaque fenêtre : est-ce la voix suivie ?
            with contextlib.suppress(Exception):  # (la connexion se ferme à la fin du tour : normal)
                while True:
                    chunk = await mic.get()
                    voiced.append(is_voice(chunk))
                    await stt.send_audio(chunk)
                    if voice and mic.position - start >= window and len(voiced) % step == 0:
                        recent = mic.between(mic.position - window, mic.position)
                        owners.append(await asyncio.to_thread(voice.is_mine, recent))
                        # Deux fenêtres de suite qui ne ressemblent pas à la voix du début : la voix a changé.
                        if len(owners) >= 3 and owners[-1] == owners[-2] != owners[0]:
                            # Le changement est dans la fenêtre d'avant : à la dernière pause, sinon en son milieu.
                            pause = next_phrase(voiced, len(voiced) - step - window)
                            cut = first + pause - 3 if pause is not None else mic.position - step - window // 2
                            print(f"   ✂️  la voix change à {seconds(cut - start):.1f} s : je coupe la phrase ici")
                            asyncio.create_task(end_turn())
                            return

        sender = asyncio.create_task(send_mic())

        async for msg in stt:
            if msg["type"] == "text":
                words.append((msg.get("start_s", 0), msg["text"]))
                print("   ...", said(cut))
                if on_text:
                    await on_text(said(cut))

            elif msg["type"] == "step" and not flushing:
                end_prob = {v["horizon_s"]: v["inactivity_prob"] for v in msg["vad"]}.get(END_HORIZON, 0)
                # Fin du tour : la voix s'est tue... après des mots, ou sans qu'aucun mot n'ait été compris.
                if end_prob > END_THRESHOLD and (words or msg.get("total_duration_s", 0) > NO_WORDS_S):
                    await end_turn()

            elif msg["type"] == "flushed":  # tout le texte est arrivé
                break

        sender.cancel()

    if cut is None:
        # Gradium décide un peu après la fin de la phrase : si une nouvelle phrase a commencé entre-temps
        # (après une pause, et après le dernier mot compris), elle appartient à la demande suivante.
        last_word = chunks_in(max((t for t, _ in words), default=0)) - (first - start)
        pause = next_phrase(voiced, last_word)
        cut = first + pause - 3 if pause is not None else mic.position
    return said(cut), cut



class AssemblyAISession:
    """Une connexion AssemblyAI pour toute une session « hey browser ». Le micro y part en continu (depuis
    l'ouverture), et chaque phrase terminée (message `Turn` avec end_of_turn) arrive dans la file `turns` :
    c'est la demande suivante, dans l'ordre. On n'utilise pas les horaires des mots d'AssemblyAI : mesuré, ils
    sont décalés d'environ 1 s par rapport à l'audio envoyé."""

    def __init__(self, mic):
        self.mic = mic
        self.turns = asyncio.Queue()
        self.on_text = None  # le texte en cours de phrase, pour l'afficher (réglé par next_turn)
        self.ws = None
        self.tasks = []

    async def open(self):
        params = {"sample_rate": SAMPLE_RATE, "encoding": "pcm_s16le", "speech_model": ASSEMBLYAI_MODEL}
        self.ws = await connect(f"{ASSEMBLYAI_URL}?{urlencode(params)}",
                                additional_headers={"Authorization": os.environ["ASSEMBLYAI_API_KEY"]})
        self.origin = self.mic.position  # (l'audio envoyé part d'ici : juste après « hey browser »)
        self.tasks = [asyncio.create_task(self.send()), asyncio.create_task(self.receive())]

    async def send(self):
        """Envoie tout le micro, depuis l'ouverture. AssemblyAI refuse les envois de moins de 50 ms :
        on regroupe les morceaux de 32 ms par deux (64 ms)."""
        position = self.origin
        with contextlib.suppress(Exception):  # (connexion fermée : fin de la session)
            while True:
                pair = [await self.mic.at(position), await self.mic.at(position + 1)]
                position += 2
                await self.ws.send(np.concatenate(pair).tobytes())

    async def receive(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if msg.get("type") == "Turn":
                    text = msg.get("transcript", "")
                    if not msg.get("end_of_turn"):
                        if self.on_text and text:
                            await self.on_text(text)
                        continue
                    await self.turns.put(text)
                elif "error" in msg:
                    raise RuntimeError(f"AssemblyAI : {msg['error']}")
            raise RuntimeError("AssemblyAI : connexion fermée")
        except Exception as error:  # (next_turn la fera remonter : la session sera fermée, puis rouverte au besoin)
            await self.turns.put(error)

    async def close(self):
        with contextlib.suppress(Exception):
            await self.ws.send(json.dumps({"type": "Terminate"}))  # (sinon AssemblyAI la compte encore ouverte)
            await self.ws.close()
        for task in self.tasks:
            task.cancel()


async def next_turn(mic, start, on_text=None, stream=None):
    """Étape 2, version AssemblyAI : attend la prochaine phrase terminée par AssemblyAI (celle qui a commencé
    vers `start`). Renvoie (texte, position de la fin de la demande). Pendant l'attente, Silero continue de lire
    le micro : si la voix s'est tue depuis longtemps sans qu'AssemblyAI annonce de phrase (toux, bruit), on abandonne."""
    own = stream is None
    if own:  # (hors session : une connexion pour cette seule demande)
        stream = AssemblyAISession(mic)
        await stream.open()
    stream.on_text = on_text

    async def silence_watch():
        quiet = 0
        while seconds(quiet) < NO_WORDS_S + 1:
            quiet = 0 if is_voice(await mic.get()) else quiet + 1

    try:
        turn = asyncio.create_task(stream.turns.get())
        watch = asyncio.create_task(silence_watch())
        await asyncio.wait({turn, watch}, return_when=asyncio.FIRST_COMPLETED)
        watch.cancel()
        if not turn.done():
            turn.cancel()
            print("   (voix entendue, mais aucune phrase : bruit ?)")
            return "", mic.position  # que du bruit : pas de phrase
        text = turn.result()
        if isinstance(text, Exception):
            raise text
        # La fin de la demande : là où Silero en est (on ne rend rien au micro : la connexion reçoit tout
        # en continu, la phrase suivante arrivera de toute façon dans la file).
        return text, mic.position
    finally:
        stream.on_text = None
        if own:
            await stream.close()
