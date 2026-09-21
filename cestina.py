"""Skloňování jmen sázkařů pro hlášky bota: vokativ (oslovení) a genitiv.

Pravidla pokrývají běžná česká jména a přezdívky; co pravidla netrefí,
patří do IRREGULAR jako (5. pád, 2. pád). Víceslovné jméno skloňuje
jen první slovo, jméno končící tečkou nebo číslem se nechává být.
"""

IRREGULAR: dict[str, tuple[str, str]] = {
    "Schejby": ("Schejby", "Schejbyho"),
    "Ejdm": ("Adame", "Adama"),
    "Kunc": ("Kune", "Kunce"),
    "Bejdžin": ("Bagoši", "Bejdžina"),
    "Žoužel": ("Žouželko", "Žouželky"),
}

SOFT = "sšzžcčjřďťň"  # měkké souhlásky: 5. p. -i, 2. p. -e
VOWELS = "aeiouyáéěíóúůý"


def _split(name: str) -> tuple[str, str]:
    head, _, tail = name.strip().partition(" ")
    return head, (" " + tail if tail else "")


def vokativ(name: str) -> str:
    """Štěpa -> Štěpo, Radek -> Radku, Pavel -> Pavle, Psík -> Psíku, Martin -> Martine."""
    if name in IRREGULAR:
        return IRREGULAR[name][0]
    w, rest = _split(name)
    low = w.lower()
    if not low or not low[-1].isalpha():
        return name
    if low.endswith("a"):
        out = w[:-1] + "o"
    elif low.endswith("ek") and len(w) > 3:
        out = w[:-2] + "ku"
    elif low.endswith("el") and len(w) > 3:
        out = w[:-2] + "le"
    elif low[-1] in VOWELS:
        out = w  # Tomio, Ondry
    elif low.endswith("ch") or low[-1] in "kgh":
        out = w + "u"
    elif low[-1] in SOFT:
        out = w + "i"
    elif low.endswith("r") and low[-2] not in VOWELS:
        out = w[:-1] + "ře"  # Petr -> Petře
    else:
        out = w + "e"
    return out + rest


def genitiv(name: str) -> str:
    """Štěpa -> Štěpy, Radek -> Radka, Pavel -> Pavla, Tomio -> Tomia, Martin -> Martina."""
    if name in IRREGULAR:
        return IRREGULAR[name][1]
    w, rest = _split(name)
    low = w.lower()
    if not low or not low[-1].isalpha():
        return name
    if low.endswith("a"):
        out = w[:-1] + "y"
    elif low.endswith("ek") and len(w) > 3:
        out = w[:-2] + "ka"
    elif low.endswith("el") and len(w) > 3:
        out = w[:-2] + "la"
    elif low.endswith("o"):
        out = w[:-1] + "a"
    elif low[-1] in VOWELS:
        out = w  # nesklonné
    elif low[-1] in SOFT:
        out = w + "e"
    else:
        out = w + "a"
    return out + rest


if __name__ == "__main__":
    import json
    import pathlib

    players = json.loads((pathlib.Path(__file__).parent / "data/players.json").read_text())
    for n in players.values():
        print(f"{n:10} {vokativ(n):12} {genitiv(n)}")
