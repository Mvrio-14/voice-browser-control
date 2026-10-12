"""ÉVALUATION (1/2) : enregistrer des situations réelles, une fois pour toutes.

Les sites changent à chaque chargement (YouTube réordonne ses résultats, ajoute des pubs...) : impossible de
comparer deux réglages honnêtement en direct. On enregistre donc des « situations » (capture numérotée +
éléments + état de la page) dans eval/fixtures/, et run.py les rejoue autant de fois qu'on veut.

Lancer :  .venv/Scripts/python voice_click/eval/capture.py
(ouvre un Chrome de test séparé, sur un profil temporaire)
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
sys.stdout.reconfigure(encoding="utf-8")
from dotenv import load_dotenv

load_dotenv(HERE.parent.parent / ".env")
from playwright.async_api import async_playwright

import browser
import panel
from see import see

FIXTURES = HERE / "fixtures"
REJECT_COOKIES = "() => [...document.querySelectorAll('button')].find(b => /reject all|tout refuser|refuser tout/i.test(b.innerText))?.click()"
TOP_OF = "t => { const a = [...document.querySelectorAll('a#video-title')].find(a => a.textContent.includes(t)); return a ? a.getBoundingClientRect().top + scrollY : null; }"


async def save(page, name):
    elements, screenshot, state = await see(page)
    (FIXTURES / f"{name}.jpg").write_bytes(screenshot)
    (FIXTURES / f"{name}.json").write_text(json.dumps({"url": page.url, "state": state, "elements": elements},
                                                      ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"✅ {name} : {len(elements)} éléments")


async def scroll_to(page, y):
    await page.evaluate(f"scrollTo(0, {max(0, y)})")
    await page.wait_for_timeout(1200)


async def main():
    FIXTURES.mkdir(exist_ok=True)
    async with async_playwright() as p:
        chrome = await browser.open_chrome(p, "about:blank", port=9333, profile=Path(tempfile.mkdtemp(prefix="vc-eval-")))
        context = chrome.contexts[0]
        await panel.install(context, "Ctrl+Maj+Espace")
        page = context.pages[0]

        # YouTube, résultats « GreenCode » : la vidéo au chat (Scratch) et le piège « LLM from Scratch »
        await page.goto("https://www.youtube.com/results?search_query=GreenCode")
        await page.wait_for_timeout(4000)
        await page.evaluate(REJECT_COOKIES)
        await page.wait_for_timeout(5000)
        llm, cat = await page.evaluate(TOP_OF, "LLM"), await page.evaluate(TOP_OF, "SCRATCH")
        print("positions : LLM", llm, "· chat", cat)
        if llm is not None:
            await scroll_to(page, llm - 150)
            await save(page, "yt_greencode_llm")
        if cat is not None:
            await scroll_to(page, cat - 250)
            await save(page, "yt_greencode_chat")
        if llm is not None and cat is not None and cat - llm > 1100:
            await scroll_to(page, (llm + cat) / 2 - 200)
            await save(page, "yt_greencode_entre")
        await scroll_to(page, 0)
        await save(page, "yt_greencode_haut")

        # YouTube, résultats « animaux drôles »
        await page.goto("https://www.youtube.com/results?search_query=animaux+droles")
        await page.wait_for_timeout(5000)
        await save(page, "yt_animaux")

        # Wikipédia : texte, image, champ de recherche
        await page.goto("https://fr.wikipedia.org/wiki/Alan_Turing")
        await page.wait_for_timeout(3000)
        await page.evaluate(REJECT_COOKIES)
        await save(page, "wiki_turing_haut")
        await scroll_to(page, 1400)
        await save(page, "wiki_turing_milieu")

        await (await chrome.new_browser_cdp_session()).send("Browser.close")


asyncio.run(main())
