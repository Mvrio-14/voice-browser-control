"""Diagnostic : la connexion AssemblyAI de session, avec le vrai micro, sans navigateur.
Lance-le, dis 2 ou 3 commandes espacées (« descends un peu »... « mets en pause »...), puis attends : il s'arrête seul après 30 s.
Il affiche chaque message d'AssemblyAI et ce que le code en fait, avec les horaires (en secondes depuis le lancement).

    .venv/Scripts/python voice_click/eval/diag_assemblyai.py
"""
import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")
import listen

DURATION_S = 30


async def main():
    with listen.Microphone() as mic:
        await asyncio.sleep(0.5)
        mic.rewind(mic.base + len(mic.audio))  # (on part de maintenant)
        stream = listen.AssemblyAISession(mic)

        # On intercepte les messages bruts (listen.py les lit avec json.loads), pour les afficher au passage.
        class ShowingJson:
            @staticmethod
            def loads(raw):
                msg = json.loads(raw)
                if msg.get("type") == "Turn":
                    words = msg.get("words") or []
                    span = f"mots {words[0]['start'] / 1000:.2f}→{words[-1]['end'] / 1000:.2f} s" if words else "sans mots"
                    print(f"   [AssemblyAI] Turn fin={msg.get('end_of_turn')} « {msg.get('transcript', '')} » ({span}"
                          f" depuis l'ouverture) ; micro lu jusqu'à {listen.seconds(mic.position - stream.origin):.2f} s")
                else:
                    print(f"   [AssemblyAI] {msg.get('type', msg)}")
                return msg
            dumps = staticmethod(json.dumps)
        listen.json = ShowingJson

        await stream.open()
        print(f"Connexion ouverte. Parle (pendant {DURATION_S} s)...\n")

        async def loop():
            while True:
                text, audio = await listen.listen(mic, DURATION_S, stream=stream)
                if text is None:
                    return
                print(f"➜ listen() renvoie « {text} » ({len(audio) / 16000:.2f} s d'audio)\n")

        # (le code accepte une phrase si elle finit APRÈS le début de la voix : on affiche les deux)
        real_next_turn = listen.next_turn

        async def next_turn_shown(mic_, start, on_text=None, stream_=None):
            print(f"   [Silero] voix entendue à {listen.seconds(start - stream.origin):.2f} s depuis l'ouverture")
            text, end = await real_next_turn(mic_, start, on_text, stream_)
            print(f"   [code] phrase retenue : « {text} », fin à {listen.seconds(end - stream.origin):.2f} s"
                  + ("   ← vide : abandon après silence" if not text else ""))
            return text, end
        listen.next_turn = next_turn_shown

        try:
            await asyncio.wait_for(loop(), DURATION_S)
        except TimeoutError:
            pass
        await stream.close()
        print("\nFin.")


asyncio.run(main())
