"""ÉVALUATION (2/2) : rejouer les demandes sur les situations enregistrées, et compter les bonnes cibles.

Compare deux méthodes, sur exactement les mêmes situations :
  - « 1 temps »  : la cible choisie par decide.py seule (si sa confiance atteint le seuil d'act.py)
  - « 2 temps »  : decide.py, puis resolve.py (présélection + vérification de chaque candidat seul)

Lancer :  .venv/Scripts/python voice_click/eval/run.py
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
sys.stdout.reconfigure(encoding="utf-8")
from dotenv import load_dotenv

load_dotenv(HERE.parent.parent / ".env")

from act import MIN_CONFIDENCE
from cases import CASES
from decide import decide
from resolve import TARGET_ACTIONS, resolve

FIXTURES = HERE / "fixtures"


def run_case(case):
    situation, request, expected_action, expected = case
    data = json.loads((FIXTURES / f"{situation}.json").read_text(encoding="utf-8"))
    screenshot = (FIXTURES / f"{situation}.jpg").read_bytes()
    elements = data["elements"]

    t0 = time.perf_counter()
    d = decide(request, elements, screenshot, data["state"], [])
    t1 = time.perf_counter()
    one = d["target"] if d["action"] in TARGET_ACTIONS and d["target_confidence"] >= MIN_CONFIDENCE else None
    r = resolve(request, d, elements, screenshot)
    t2 = time.perf_counter()
    two = r["target"] if d["action"] in TARGET_ACTIONS else None

    accepted = expected if isinstance(expected, set) else {expected}
    return {"case": case, "action_ok": d["action"] == expected_action or expected is None,
            "one": one, "two": two, "one_ok": one in accepted, "two_ok": two in accepted,
            "checked": r.get("shortlist", []), "t_decide": t1 - t0, "t_resolve": t2 - t1, "action": d["action"]}


def main():
    with ThreadPoolExecutor(6) as pool:  # (plusieurs cas à la fois, pour aller plus vite)
        results = list(pool.map(run_case, CASES))

    names = {}
    for r in results:
        situation, request, expected_action, expected = r["case"]
        mark = lambda ok: "✅" if ok else "❌"
        shown = "/".join(sorted(str(x) for x in expected)) if isinstance(expected, set) else str(expected)
        checked = ",".join(r["checked"])
        print(f"{situation:18} « {request[:52]:52} » attendu {shown:7} | "
              f"1 temps {mark(r['one_ok'])} {str(r['one']):5} | 2 temps {mark(r['two_ok'])} {str(r['two']):5} "
              f"| action {r['action']:9}{'' if r['action_ok'] else ' ⚠️'} | présélection {checked}")

    n = len(results)
    one, two = sum(r["one_ok"] for r in results), sum(r["two_ok"] for r in results)
    wrong_one = sum(1 for r in results if not r["one_ok"] and r["one"] is not None)
    wrong_two = sum(1 for r in results if not r["two_ok"] and r["two"] is not None)
    print(f"\nScore 1 temps : {one}/{n} ({wrong_one} clics sur un MAUVAIS élément)")
    print(f"Score 2 temps : {two}/{n} ({wrong_two} clics sur un MAUVAIS élément)")
    print(f"Temps moyen : décision {sum(r['t_decide'] for r in results) / n:.2f} s"
          f" + vérification {sum(r['t_resolve'] for r in results) / n:.2f} s")


main()
