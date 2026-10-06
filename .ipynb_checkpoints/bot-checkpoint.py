"""Solo D&D: an AI Dungeon Master plus an AI-controlled party mate.

Setup:  pip install google-genai
        export GEMINI_API_KEY=...
Run:    python dnd_party.py

Dice and hit points are handled by real Python code, exposed to the DM as
tools, so the DM can't fudge rolls or forget how hurt you are.
"""
import os
import random
import re
import time

from google import genai
from google.genai import errors, types

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

YEL, CYN, DIM, RST = "\033[33m", "\033[36m", "\033[2m", "\033[0m"
ABILITIES = ["STR", "DEX", "CON", "INT", "WIS", "CHA"]

# Highest rolled score goes to the first ability in the priority list.
CLASSES = {
    "fighter": {"hit_die": 10, "ac": 18, "priority": ["STR", "CON", "DEX", "WIS", "CHA", "INT"],
                "gear": "longsword, shield, chain mail"},
    "rogue":   {"hit_die": 8, "ac": 14, "priority": ["DEX", "INT", "CON", "WIS", "CHA", "STR"],
                "gear": "shortsword, dagger, shortbow, thieves' tools, leather armor"},
    "wizard":  {"hit_die": 6, "ac": 12, "priority": ["INT", "DEX", "CON", "WIS", "CHA", "STR"],
                "gear": "quarterstaff, spellbook, component pouch"},
    "cleric":  {"hit_die": 8, "ac": 18, "priority": ["WIS", "CON", "STR", "CHA", "DEX", "INT"],
                "gear": "mace, shield, scale mail, holy symbol"},
    "ranger":  {"hit_die": 10, "ac": 14, "priority": ["DEX", "WIS", "CON", "STR", "INT", "CHA"],
                "gear": "longbow, two shortswords, leather armor"},
}
RACES = ["human", "elf", "dwarf", "halfling", "half-orc", "gnome"]
MATE_NAMES = ["Brindle", "Sorrel", "Kestrel", "Maro", "Tilda", "Voss", "Pip", "Ashe"]
PERSONALITIES = [
    "dry-witted and sarcastic, but fiercely loyal",
    "cheerful, endlessly curious, and a little reckless",
    "gruff and cautious, speaks in short sentences",
    "warm, superstitious, and always thinking about snacks",
    "earnest and idealistic, hates seeing anyone bullied",
    "mercenary and wry, but secretly sentimental",
]

PARTY = []  # [player_character, party_mate]


# ---------- Rules engine (called by the DM as tools) ----------

DICE_RE = re.compile(r"^(\d*)d(\d+)(?:k([hl])(\d+))?([+-]\d+)?$")


def mod(score):
    return (score - 10) // 2


def roll_dice(notation: str, reason: str) -> dict:
    """Roll dice with real randomness. Always use this for any random outcome.

    Args:
        notation: Dice notation like "1d20+3", "2d6", "d20-1". Use "2d20kh1+4"
            for advantage (keep highest) and "2d20kl1+4" for disadvantage.
        reason: Short description, e.g. "Thorn attack roll vs goblin".
    """
    m = DICE_RE.match(notation.replace(" ", "").lower())
    if not m:
        return {"error": f"Bad dice notation: {notation}"}
    count, sides, bonus = int(m[1] or 1), int(m[2]), int(m[5] or 0)
    if not (1 <= count <= 100 and 2 <= sides <= 1000):
        return {"error": "Dice count or size out of range"}
    rolls = [random.randint(1, sides) for _ in range(count)]
    kept = rolls
    if m[3]:
        kept = sorted(rolls, reverse=(m[3] == "h"))[: int(m[4])]
    total = sum(kept) + bonus

    result = {"rolls": rolls, "kept": kept, "modifier": bonus, "total": total}
    flag = ""
    if sides == 20 and len(kept) == 1:
        if kept[0] == 20:
            result["note"], flag = "NATURAL 20", " ✨ NAT 20!"
        elif kept[0] == 1:
            result["note"], flag = "NATURAL 1", " 💀 NAT 1!"
    shown = f"{rolls}" + (f" keep {kept}" if kept != rolls else "")
    print(f"{DIM}🎲 {reason}: {notation} → {shown}{bonus:+d} = {total}{flag}{RST}")
    return result


def change_hp(character_name: str, amount: int, reason: str) -> dict:
    """Change a party member's hit points. Negative = damage, positive = healing.
    Call this every time a party member takes damage or is healed.

    Args:
        character_name: Exact name of the party member.
        amount: Signed HP change.
        reason: Short description, e.g. "goblin arrow".
    """
    for c in PARTY:
        if c["name"].lower() == character_name.lower():
            c["hp"] = max(0, min(c["max_hp"], c["hp"] + amount))
            print(f"{DIM}❤️  {c['name']}: {amount:+d} ({reason}) → {c['hp']}/{c['max_hp']} HP{RST}")
            return {"name": c["name"], "hp": c["hp"], "max_hp": c["max_hp"],
                    "unconscious": c["hp"] == 0}
    return {"error": f"No party member named {character_name}"}


# ---------- Characters ----------

def make_character(name, race, cls, personality=None):
    spec = CLASSES[cls]
    # 4d6, drop lowest, six times; best scores go to the class's key abilities.
    scores = sorted((sum(sorted(random.randint(1, 6) for _ in range(4))[1:]) for _ in range(6)),
                    reverse=True)
    stats = dict(zip(spec["priority"], scores))
    hp = spec["hit_die"] + mod(stats["CON"])
    return {"name": name, "race": race, "class": cls, "stats": stats, "hp": hp, "max_hp": hp,
            "ac": spec["ac"], "gear": spec["gear"], "personality": personality}


def sheet(c):
    scores = "  ".join(f"{a} {c['stats'][a]} ({mod(c['stats'][a]):+d})" for a in ABILITIES)
    return (f"{c['name']}: level 1 {c['race']} {c['class']}\n"
            f"  HP {c['hp']}/{c['max_hp']}   AC {c['ac']}\n"
            f"  {scores}\n"
            f"  Gear: {c['gear']}")


def pick_class(prompt, default):
    options = "/".join(CLASSES)
    while True:
        choice = input(f"{prompt} ({options}) [{default}]: ").strip().lower() or default
        if choice in CLASSES:
            return choice
        print("  Pick one from the list.")


# ---------- Prompts ----------

def dm_prompt():
    sheets = "\n\n".join(sheet(c) for c in PARTY)
    return f"""You are the Dungeon Master for a two-person D&D 5e-style adventure. The party is the
human player's character, {PARTY[0]['name']}, and the AI-controlled party mate, {PARTY[1]['name']}.
Both are level 1.

PARTY SHEETS (starting state):
{sheets}

RULES FOR YOU:
- Use the roll_dice tool for EVERY random outcome: attacks, ability checks, saving throws, damage,
  initiative, loot tables, random encounters. Never invent a roll result. Roll for the player's
  character and the party mate too, using the modifiers on their sheets (add proficiency +2 where it
  plausibly applies). Roll enemy dice as well.
- Use the change_hp tool whenever a party member is hurt or healed. Track enemy HP yourself.
- Never decide what the player's character says, thinks, or does. Narrate the world and consequences,
  then ask what they do.
- The party mate is played by another AI. When told what it does or says, resolve it fairly and
  weave it into the scene, but don't rewrite its intent.
- Keep narration vivid but tight: 1-3 short paragraphs. Give the player real choices and let clever
  ideas work. Failure should create complications, not dead ends. Danger is real and death is
  possible, but telegraph lethal threats first.
- Pace the story: hooks, exploration, social scenes, and combat. Combat uses initiative and rounds.
- Stay in character as the DM. If the player asks an out-of-game question, answer briefly and
  return to the scene."""


def mate_prompt(mate, hero):
    return f"""You are {mate['name']}, a {mate['race']} {mate['class']} adventuring with {hero['name']}
(a {hero['race']} {hero['class']}), in a D&D game run by a Dungeon Master.

Personality: {mate['personality']}.

Your sheet:
{sheet(mate)}

RULES:
- Reply in first person as {mate['name']}, 1-3 sentences, mixing dialogue and action,
  like: *checks the door for traps* "Quiet. Something's breathing in there."
- Declare what you attempt. NEVER narrate the outcome, roll dice, or describe the world beyond what
  your character can perceive: the DM resolves everything.
- Never speak, act, or decide for {hero['name']}.
- Use your class abilities sensibly, have opinions, banter, disagree sometimes, and care about the
  party. Don't hog the spotlight; the player is the protagonist.
- You only know what your character has seen or heard."""


# ---------- Model plumbing ----------

def ask(chat, text, retries=5):
    """Send a message, retrying on 5xx overload errors with exponential backoff."""
    delay = 2
    for attempt in range(retries):
        try:
            return (chat.send_message(text).text or "").strip()
        except errors.ServerError as e:
            if attempt == retries - 1:
                raise
            print(f"{DIM}⚠️  Server busy ({e.code}), retrying in {delay}s...{RST}")
            time.sleep(delay)
            delay *= 2


def say(color, label, text):
    print(f"\n{color}{label}{RST}\n{text}\n")


def show_help():
    print("""
Commands:
  /sheet        show both character sheets
  /roll 1d20+3  roll dice yourself
  /recap        ask the DM for a story recap
  /help         show this list
  /quit         leave the game
Anything else is what your character says or does.
""")


# ---------- Main game ----------

def main():
    client = genai.Client()

    print("🐉 Solo D&D: AI Dungeon Master + AI party mate\n")
    name = input("Your character's name [Aria]: ").strip() or "Aria"
    race = input(f"Race ({'/'.join(RACES)}) [human]: ").strip().lower() or "human"
    cls = pick_class("Your class", "fighter")
    hero = make_character(name, race, cls)

    mate_cls = pick_class("Party mate's class", "cleric")
    mate = make_character(random.choice(MATE_NAMES), random.choice(RACES), mate_cls,
                          random.choice(PERSONALITIES))
    PARTY.extend([hero, mate])

    print("\n" + sheet(hero) + "\n\n" + sheet(mate) + f"\n  ({mate['personality']})\n")
    premise = input("Campaign vibe (blank = surprise me): ").strip() or "your choice; surprise us"

    dm = client.chats.create(
        model=MODEL,
        config=types.GenerateContentConfig(
            system_instruction=dm_prompt(), tools=[roll_dice, change_hp], temperature=1.0),
    )
    mate_chat = client.chats.create(
        model=MODEL,
        config=types.GenerateContentConfig(
            system_instruction=mate_prompt(mate, hero), temperature=1.0),
    )

    unseen = []  # events the party mate hasn't been told about yet

    def mate_acts():
        """Party mate declares an action, then the DM resolves it."""
        update = "\n\n".join(unseen)
        unseen.clear()
        line = ask(mate_chat, f"{update}\n\nWhat does {mate['name']} say or do?")
        say(CYN, f"🗡️  {mate['name']} ({mate['class']})", line)
        outcome = ask(dm, f"[Party mate {mate['name']}]: {line}\n\nResolve this (roll dice if needed), "
                          f"then hand the turn back to {hero['name']}.")
        say(YEL, "📜 Dungeon Master", outcome)
        unseen.append(f"Dungeon Master (resolving your last action): {outcome}")

    show_help()
    try:
        opening = ask(dm, f"Begin the adventure. Campaign vibe: {premise}. Set the opening scene, "
                          f"introduce a hook, and end by asking what the party does.")
        say(YEL, "📜 Dungeon Master", opening)
        unseen.append(f"Dungeon Master: {opening}")
        mate_acts()

        while True:
            text = input(f"{hero['name']}> ").strip()
            low = text.lower()
            if not text:
                continue
            if low in ("/quit", "/exit", "quit", "exit"):
                break
            if low == "/help":
                show_help()
            elif low == "/sheet":
                print("\n" + "\n\n".join(sheet(c) for c in PARTY) + "\n")
            elif low.startswith("/roll"):
                roll_dice(text[5:].strip() or "1d20", "Manual roll")
            elif low == "/recap":
                say(YEL, "📜 Recap", ask(dm, "Give a short recap of the story so far and our current "
                                            "situation, including the party's current HP."))
            else:
                narration = ask(dm, f"[{hero['name']}, the player]: {text}")
                say(YEL, "📜 Dungeon Master", narration)
                unseen.append(f"{hero['name']} (the player): {text}")
                unseen.append(f"Dungeon Master: {narration}")
                mate_acts()
    except (EOFError, KeyboardInterrupt):
        print()
    except Exception as e:
        print(f"❌ Error: {e}")

    print("🎲 Until next session, adventurer!")


if __name__ == "__main__":
    main()