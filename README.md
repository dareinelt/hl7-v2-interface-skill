# HL7 v2 Interface Skill

An [Agent Skill](https://code.claude.com/docs/en/skills) for importing and exporting
**HL7 v2.x interface data** between medical systems — with bidirectional mapping to
**FHIR R4** and a bridge to German **GDT/xDT**.

Written for Python 3.10+, standard library only. No parser install required.

```text
ADT   Patient, Encounter, Practitioner
ORU   DiagnosticReport, Observation, ServiceRequest (results)
ORM   ServiceRequest (orders)
SIU   Appointment, Practitioner, Location, Device (scheduling)
MDM   DocumentReference, Encounter (documents with ED content)
GDT   6301 / 6302 / 6310  <->  FHIR  <->  HL7 v2
```

## Why

Most HL7 integrations fail on the same handful of things: MLLP framing read until
EOF, acknowledgements sent before the commit, MSH-2 split on the repetition
character, absent fields turned into explicit nulls, or an invented LOINC code. This
skill packages the working procedure plus a reference implementation that refuses to
guess.

## Contents

| Path | What it is |
|------|------------|
| `SKILL.md` | The skill itself: scope, operating rules, import/export workflows, pitfalls, verification checklist |
| `references/` | Format reference, message/field tables, MLLP & ACK semantics, FHIR mapping, SIU/MDM mapping, GDT bridge, Python ecosystem, production checklist |
| `scripts/hl7core.py` | ER7 parser/serializer, validation, ACK builder, MLLP codec, TLS client |
| `scripts/hl7_tool.py` | CLI: `parse`, `validate`, `ack`, `send`, `serve`, `frame`, `unframe` |
| `scripts/hl7_fhir_map.py` | HL7 v2 <-> FHIR R4 for ADT, ORU, ORM/OML, SIU, MDM |
| `scripts/hl7_siu_mdm.py` | Appointment and DocumentReference mapping |
| `scripts/gdt_bridge.py` | GDT <-> FHIR <-> HL7 (uses the `gdt-interface` skill for byte-exact GDT) |
| `assets/` | Synthetic messages: ADT, ORU, ORM, SIU, MDM, GDT, FHIR bundle |
| `tests/` | 31 executable expectations for parser, mapper and GDT bridge |

## Install

Claude Code, GitHub Copilot, Cursor, Codex and other tools that read `SKILL.md`
discover a skill by its folder.

**Personal skills** (all projects):

```bash
git clone https://github.com/dareinelt/hl7-v2-interface-skill.git \
  ~/.copilot/skills/hl7-v2-interface
```

Use `~/.claude/skills/` for Claude Code, or the equivalent folder of your agent.

**Project skills** (one repository):

```bash
mkdir -p .claude/skills
git clone https://github.com/dareinelt/hl7-v2-interface-skill.git \
  .claude/skills/hl7-v2-interface
```

The GDT bridge needs the sibling [`gdt-interface`](https://github.com/dareinelt) skill,
or pass `--gdt-tool /path/to/gdt_tool.py`.

## Quick start

```bash
# Inspect and validate a message
python scripts/hl7_tool.py parse assets/adt_a01.hl7 --tree
python scripts/hl7_tool.py validate assets/oru_r01.hl7 --strict

# Build an acknowledgement
python scripts/hl7_tool.py ack assets/adt_a01.hl7 --code AE --text "Patient unknown"

# MLLP client and listener
python scripts/hl7_tool.py send 127.0.0.1 6661 --file assets/adt_a01.hl7
python scripts/hl7_tool.py serve --port 6661 --out inbox

# Mapping, both directions
python scripts/hl7_fhir_map.py hl7-to-fhir assets/oru_r01.hl7 --out bundle.json
python scripts/hl7_fhir_map.py fhir-to-hl7 bundle.json --type ORU^R01 --out message.hl7
python scripts/hl7_fhir_map.py hl7-to-fhir assets/siu_s12.hl7 --out appointment.json
python scripts/hl7_fhir_map.py hl7-to-fhir assets/mdm_t02.hl7 --out document.json
```

Use as a library:

```python
import sys
sys.path.insert(0, "skills/hl7-v2-interface/scripts")

from hl7core import Message, build_ack, validate
from hl7_fhir_map import hl7_to_fhir

message = Message.parse(open("oru.hl7", "rb").read())
for issue in validate(message, strict=True):
    print(issue)

bundle = hl7_to_fhir(message, bundle_type="transaction")
ack = build_ack(message, "AA")
```

## Design decisions

- **Lossless read.** `Message.to_er7()` reproduces the input; empty and explicitly
  null (`""`) fields stay distinct.
- **No invented data.** Unknown identifier systems, codes and units produce a warning
  and an omitted element, never a guess.
- **ACK after commit.** The listener validates, archives, then answers `AA`; errors get
  `AE` with an `ERR` segment and no patient data.
- **Deterministic references.** `fullUrl` is `uuid5` over the message control ID, so a
  replay produces identical references.
- **Deliberate lossiness.** FHIR -> HL7 only restores fields the mapper owns; the
  boundaries are documented per message type.

## Tests

```bash
python -m unittest discover -s tests -v
```

CI runs the suite on Python 3.10, 3.11 and 3.12.

## Scope and limits

In scope: ADT, ORU, ORM/OML, SIU, MDM, MLLP, ACK/NAK, GDT 6301/6302/6310.

Out of scope: HL7 v3/CDA documents, DICOM, X12, NCPDP, BAR/DFT accounting,
conformance-profile validation (use HAPI, `hl7apy` or your interface engine).

Not a medical device. Validate every mapping against the receiving system and the
negotiated interface contract before production use.

## License

MIT — see [LICENSE](LICENSE).
