"""
Guess a part's category from its Description (plus a handful of keywords).

Rules are matched in order; the first hit wins. More specific keywords are
placed earlier so they aren't shadowed by broader ones later in the list
(e.g. "Transistor", "Diode"). Parts that match no rule stay "Uncategorized"
for an admin to sort manually in the category tree.
"""
import re
from collections import Counter

import click

from app.extensions import db
from app.models import Category, Part

# Sub-categories added on top of the base seeded tree: (parent name, name)
EXTRA_CATEGORIES = [
    ("Integrated Circuits", "Data Converters"),
    ("Integrated Circuits", "Analog Switch & Mux"),
    ("Integrated Circuits", "Driver ICs"),
    ("Integrated Circuits", "Power Management (Misc)"),
    ("Integrated Circuits", "Clock & Timer"),
    ("Integrated Circuits", "Audio ICs"),
    ("Passive Components", "Fuse & PTC Protection"),
]

# (regex, (top-level category name, sub-category name or None)); first match wins.
RULES = [
    (r"Phototransistors?", ("Optoelectronics", "Photodiodes/Phototransistors")),
    (r"\bLEDs?\b", ("Optoelectronics", "LED")),
    (r"Optocouplers?|Opto\.\s*Relay", ("Optoelectronics", "Optocouplers")),
    (r"Isolators?", ("Optoelectronics", "Optocouplers")),
    (r"PT100|Motion Tracking|Hall Switch|Ambient Light Sensor|Capacitance Sensor"
     r"|Temperature Sensor|Pressure Sensor|Humidity|\bSensors?\b|Seneor",
     ("Sensors", None)),
    (r"\bAudio\b", ("Integrated Circuits", "Audio ICs")),
    (r"MCU Supervisor", ("Integrated Circuits", "Voltage Reference & Detector")),
    (r"\bMCUs?\b", ("Integrated Circuits", "Microcontrollers")),
    (r"Comparators?", ("Integrated Circuits", "Op-Amps & Comparators")),
    (r"\bOPAMPs?\b|Current Sense Amp", ("Integrated Circuits", "Op-Amps & Comparators")),
    (r"\bLDOs?\b|Low[- ]?Dropout|Linear Regulator", ("Integrated Circuits", "Linear Regulators")),
    (r"Switch Mode Regul|DC-DC|Switched-Capacitor Voltage Convert",
     ("Integrated Circuits", "DC-DC / SMPS")),
    (r"Voltage Ref|Voltage Detector", ("Integrated Circuits", "Voltage Reference & Detector")),
    (r"Analog (Switch|Multiplexer)", ("Integrated Circuits", "Analog Switch & Mux")),
    (r"\bADC\b|\bDAC\b", ("Integrated Circuits", "Data Converters")),
    (r"Drivers?\b|H-Bridge|Sequencers?|Power Switch|Pin Driver",
     ("Integrated Circuits", "Driver ICs")),
    (r"Battery Protection|Charge Management|Energy Metering"
     r"|Reverse Polarity Protection|Battery Switchover",
     ("Integrated Circuits", "Power Management (Misc)")),
    (r"\bRTC\b|\bTimers?\b", ("Integrated Circuits", "Clock & Timer")),
    (r"EEPROM|\bFLASH\b|Flash Memory", ("Integrated Circuits", "Memory")),
    (r"RS232|RS485|GPIB|Serial Converter|WiFi|Bluetooth", ("Integrated Circuits", "Interface")),
    (r"Logic Gate|shift register|Buffer Gate|Inverter Gate|AND Gate|NAND Gate"
     r"|OR Gate|transceiver|multivibrator|I/O Expander",
     ("Integrated Circuits", "Logic")),
    (r"Crystal|Oscillator|\bOsc\.|TCXO|VCXO", ("Passive Components", "Crystals & Oscillators")),
    (r"Transformers?", ("Passive Components", "Transformers")),
    (r"Ferrite Beads?|Inductors?|Chokes?", ("Passive Components", "Inductors & Ferrite")),
    (r"MLCC|Film Cap\.|Film Capacitors?|Tantalum Capacitors?|Polymer Capacitors?"
     r"|PET Capacitors?|\bCapacitors?\b",
     ("Passive Components", "Capacitors")),
    (r"\bResistors?\b", ("Passive Components", "Resistors")),
    (r"\bFuse\b|PPTC", ("Passive Components", "Fuse & PTC Protection")),
    (r"\bTVS\b|ESD Protection|\bZener\b|Schottky|Rectifiers?|Diodes?",
     ("Discrete Semiconductors", "Diodes")),
    (r"MOSFET|JFET|\bNPN\b|\bPNP\b|Darlington|Transistors?",
     ("Discrete Semiconductors", "Transistors")),
    (r"Thyristors?|\bSCR\b|Triacs?", ("Discrete Semiconductors", "Thyristors")),
    (r"Tactile Switch|\bButtons?\b", ("Electromechanical", "Switches")),
    (r"Connectors?|Sockets?|\bZIF\b", ("Electromechanical", "Connectors")),
    (r"\bRelays?\b", ("Electromechanical", "Relays")),
    (r"\bBuzzers?\b", ("Electromechanical", None)),
]

_COMPILED_RULES = [(re.compile(pat, re.IGNORECASE), target) for pat, target in RULES]


def ensure_extra_categories():
    """Make sure the sub-categories listed in EXTRA_CATEGORIES exist (idempotent)."""
    for parent_name, name in EXTRA_CATEGORIES:
        parent = Category.query.filter_by(name=parent_name, parent_id=None).first()
        if parent is None:
            continue
        exists = Category.query.filter_by(name=name, parent_id=parent.id).first()
        if exists is None:
            siblings = Category.query.filter_by(parent_id=parent.id).count()
            db.session.add(
                Category(name=name, parent_id=parent.id, sort_order=siblings)
            )
    db.session.commit()


def build_category_lookup():
    """{(top-level category name, sub-category name or None): category_id}"""
    lookup = {}
    for c in Category.query.filter_by(parent_id=None).all():
        lookup[(c.name, None)] = c.id
        for child in c.children:
            lookup[(c.name, child.name)] = child.id
    return lookup


def classify(description, lookup):
    """Return the guessed category_id, or None if no rule matched (stays Uncategorized)."""
    if not description:
        return None
    for pattern, target in _COMPILED_RULES:
        if pattern.search(description):
            return lookup.get(target)
    return None


def run_reclassify(dry_run=False, force=False):
    ensure_extra_categories()
    lookup = build_category_lookup()
    uncategorized_id = lookup.get(("Uncategorized", None))

    query = Part.query
    if not force:
        query = query.filter(db.or_(Part.category_id.is_(None), Part.category_id == uncategorized_id))
    parts = query.all()

    by_category = Counter()
    unmatched = Counter()
    changed = 0

    for part in parts:
        cat_id = classify(part.description, lookup)
        if cat_id is None:
            unmatched[part.description or "(no description)"] += 1
            continue
        if part.category_id != cat_id:
            part.category_id = cat_id
            changed += 1
        by_category[cat_id] += 1

    if dry_run:
        db.session.rollback()
    else:
        db.session.commit()

    id_to_name = {c.id: c.path_name(" / ") for c in Category.query.all()}
    click.echo("By category ({}):".format("dry run, not written" if dry_run else "written"))
    for cat_id, cnt in sorted(by_category.items(), key=lambda kv: -kv[1]):
        click.echo(f"  {id_to_name.get(cat_id, cat_id)}: {cnt}")
    click.echo(f"Processed {len(parts)} parts, {changed} category assignments updated.")
    click.echo(f"{sum(unmatched.values())} parts still unmatched (left Uncategorized), across {len(unmatched)} distinct descriptions:")
    for desc, cnt in unmatched.most_common(30):
        click.echo(f"    [{cnt}] {desc}")
