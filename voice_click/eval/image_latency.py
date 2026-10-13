"""MESURE : envoyer la capture d'écran à chaque décision ralentit-il la réponse ? Et que perd-on sans elle ?

Chaque cas du banc d'essai (cases.py) est décidé AVEC puis SANS la capture, en alternant l'ordre,
plusieurs fois, l'un après l'autre (pas en parallèle : pour que les temps soient comparables).

Lancer :  .venv/Scripts/python voice_click/eval/image_latency.py
"""
import json
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
sys.stdout.reconfigure(encoding="utf-8")
from dotenv import load_dotenv

load_dotenv(HERE.parent.parent / ".env")

from act import MIN_CONFIDENCE
from cases import CASES
from decide import decide

FIXTURES = HERE / "fixtures"
ROUNDS = 2


def run(case, with_image):
    situation, request, expected_action, expected = case
    data = json.loads((FIXTURES / f"{situation}.json").read_text(encoding="utf-8"))
    screenshot = (FIXTURES / f"{situation}.jpg").read_bytes() if with_image else None
    t0 = time.perf_counter()
    d = decide(request, data["elements"], screenshot, data["state"], [])
    elapsed = time.perf_counter() - t0
    target = d["target"] if d["target_confidence"] >= MIN_CONFIDENCE else None
    accepted = expected if isinstance(expected, set) else {expected}
    ok = d["action"] == expected_action and (target in accepted or expected_action not in ("click", "type_text", "search"))
    return elapsed, ok


def main():
    times = {True: [], False: []}
    wins = {True: [], False: []}
    for round_ in range(ROUNDS):
        for i, case in enumerate(CASES):
            order = (True, False) if (i + round_) % 2 == 0 else (False, True)  # (on alterne qui passe en premier)
            for with_image in order:
                elapsed, ok = run(case, with_image)
                times[with_image].append(elapsed)
                wins[with_image].append(ok)
            if round_ == 0:
                print(f"  {case[1][:60]:60}  avec {'✅' if wins[True][-1] else '❌'}  sans {'✅' if wins[False][-1] else '❌'}")
    size = statistics.mean((FIXTURES / f"{c[0]}.jpg").stat().st_size for c in CASES) / 1024
    print(f"\nCapture moyenne : {size:.0f} Ko en JPEG")
    for with_image in (True, False):
        t = sorted(times[with_image])
        label = "AVEC capture" if with_image else "SANS capture"
        print(f"{label} : médiane {statistics.median(t) * 1000:.0f} ms, 90e centile {t[int(len(t) * 0.9)] * 1000:.0f} ms ; "
              f"réussite {sum(wins[with_image])}/{len(wins[with_image])}")


main()
