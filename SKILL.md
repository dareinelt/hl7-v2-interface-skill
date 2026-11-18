---
name: hl7-v2-interface
description: Import, export, parse, validate, generate and map HL7 v2.x interface data from medical systems in Python. Use for MLLP client/server work, ACK/NAK handling, ADT/ORM/ORU/MDM/SIU messages, Z-segments, HL7 v2 to FHIR R4 mapping, and GDT/xDT to FHIR/HL7 bridging between PVS, KIS, LIS, RIS, HIS and interface engines. Includes a standard-library parser, a conservative bidirectional mapper, and production rules for PHI, idempotency and audit.
license: MIT
---

# HL7 v2 Interface Data (Python)

Handle HL7 v2.x messages between medical systems: receive them over MLLP, validate
and acknowledge them correctly, map them to FHIR R4 or GDT, and generate messages
for outbound interfaces. The bundled scripts are standard-library only and run
without installing a parser.

## Scope

Use this skill for:

- ER7 (pipe-and-hat) messages over MLLP/TCP or files: ADT, ORM/OML, ORU, MDM, SIU.
- Import into an application, export from it, or bridge to FHIR R4 / GDT.
- ACK/NAK behaviour, retries, duplicates, dead-letter handling.
- German contexts where a PVS speaks GDT and the hospital speaks HL7 v2 and both
  sides need a common FHIR model.

Not covered: HL7 v3/CDA documents, DICOM, X12, NCPDP. Use a dedicated skill or
library for those.

## Operating rules

1. Treat every message as patient data. Log message type, control ID, counts and
   outcome - never field values, names, identifiers or free text.
2. Preserve the delimiters declared in MSH-1/MSH-2. Never re-encode a message with
   hard-coded `|^~\&`.
3. Acknowledge only after the business transaction is durable. `AA` before a commit
   loses messages on a crash; answer `AE`/`AR` when the receiver cannot process.
4. Use MSH-10 (message control ID) as the idempotency key. A repeated MSH-10 with
   identical content is a duplicate, not a new event.
5. Do not invent identifier systems, OIDs, LOINC/SNOMED codes, units or reference
   ranges. Leave the element out and report the gap.
6. Treat an empty field as absent and `""` as an explicit null - they mean different
   things and must survive round-trips.
7. Test with synthetic messages and a captured corpus of real vendor quirks. Never
   copy production messages into tickets or test fixtures.

## Quick start

```bash
# Inspect and validate
python scripts/hl7_tool.py parse assets/adt_a01.hl7 --tree
python scripts/hl7_tool.py validate assets/oru_r01.hl7 --strict

# Build an acknowledgement for a stored message
python scripts/hl7_tool.py ack assets/adt_a01.hl7 --code AE --text "Patient unknown"

# MLLP client (sends, waits for the ACK, checks MSA-1)
python scripts/hl7_tool.py send 127.0.0.1 6661 --file assets/adt_a01.hl7 --ack-out ack.hl7

# MLLP listener: archives each message, validates, answers AA/AE
python scripts/hl7_tool.py serve --port 6661 --out inbox

# Map both directions
python scripts/hl7_fhir_map.py hl7-to-fhir assets/oru_r01.hl7 --out bundle.json
python scripts/hl7_fhir_map.py fhir-to-hl7 bundle.json --type ORU^R01 --out message.hl7

# Scheduling and documents
python scripts/hl7_fhir_map.py hl7-to-fhir assets/siu_s12.hl7 --out appointment.json
python scripts/hl7_fhir_map.py hl7-to-fhir assets/mdm_t02.hl7 --out document.json
python scripts/hl7_fhir_map.py fhir-to-hl7 appointment.json --out siu.hl7

# GDT bridge (requires the gdt-interface skill next to this one)
python scripts/gdt_bridge.py gdt-to-fhir assets/sample-6302.gdt --out order.json
python scripts/gdt_bridge.py gdt-to-hl7 assets/sample-6302.gdt --out order.hl7
python scripts/gdt_bridge.py hl7-to-gdt assets/oru_r01.hl7 --build result.gdt
```

Use `python` on Windows and `python3` on macOS/Linux. Run from the skill directory
or put `scripts/` on `PYTHONPATH` to import `hl7core`, `hl7_fhir_map` and
`gdt_bridge` from application code.

## Import workflow

1. **Receive.** Terminate MLLP framing (`VT payload FS CR`), enforce a size limit and
   a read timeout. Never read until EOF - the sender keeps the socket open.
2. **Parse without loss.** `Message.parse()` keeps every field verbatim; check that
   `to_er7()` reproduces the input before trusting a round-trip.
3. **Validate structurally.** `validate()` catches missing MSH fields, missing PID,
   ORU without OBR/OBX and missing OBX-3. Conformance profiles need hl7apy, HAPI or
   the interface engine.
4. **De-duplicate.** Reject or ignore a repeated MSH-10. Compare a payload hash and
   treat same-key/different-payload as a conflict, not an update.
5. **Map and persist.** Map to your canonical model (FHIR here) inside a transaction.
6. **Acknowledge.** Send `AA` only after commit; `AE` for retryable business errors,
   `AR` for malformed or unsupported messages. Echo the original MSH-10 in MSA-2.
7. **Quarantine.** Store unprocessable raw messages encrypted with a retention
   policy; the DLQ is a PHI store.

## Export workflow

1. Build the canonical object first, then render HL7 from it. Do not concatenate
   strings with patient data.
2. Fill MSH-3/4/5/6 from configuration, MSH-7 from the clock, MSH-9 with
   `CODE^TRIGGER^STRUCTURE`, MSH-10 with a fresh unique ID, MSH-11 `P` (or `T` in
   test), MSH-12 with the negotiated version.
3. Send over MLLP and wait for the ACK. `AA`/`CA` is success; `AE` means retry after
   correction; `AR` means do not retry unchanged.
4. Correlate the ACK via MSA-2. A missing or mismatched MSA-2 must be treated as a
   protocol error, even when MSA-1 is `AA`.
5. Persist the outbound message, ACK and timestamps for audit. Retry with backoff and
   a bounded attempt count; never resend on an unknown ACK state without checking.

## Message essentials

| Item | Meaning |
|------|---------|
| MSH-1 / MSH-2 | Field separator and encoding characters (component, repetition, escape, subcomponent) |
| MSH-9 | Message type: `ADT^A01^ADT_A01` |
| MSH-10 | Message control ID - idempotency key, echoed in MSA-2 |
| MSH-11 / MSH-12 | Processing ID (`P`/`T`/`D`) and version (`2.5.1`) |
| PID-3 | Patient identifiers, repetitions; component 5 is the identifier type (MR, SS) |
| PID-5 | Patient name: family, given, middle, suffix, prefix |
| PV1-2 / PV1-19 | Patient class (I/O/E) and visit number |
| OBR-4 / OBR-25 | Order/report code and result status |
| OBX-2/3/5/6/11 | Value type, observation code, value, units, result status |
| SCH-25 / AIP-3 | Appointment filler status / personnel resource |
| TXA-12 / TXA-17 | Unique document number / document completion status |
| MSA-1 / MSA-2 | ACK code and echoed control ID |

Details and field tables: `references/messages.md`.

## Mapping policy (HL7 <-> FHIR)

The mapper is deliberately conservative and reports every gap as a warning:

- Identifiers: an assigning authority that is an OID becomes `urn:oid:<oid>`, a UUID
  becomes `urn:uuid:<uuid>`, everything else becomes a clearly local URI. Pass
  `--patient-system` when the feed has no authority.
- Codes: `LN` -> LOINC, `SCT` -> SNOMED CT, `I10`/`I9` -> ICD. Unknown systems keep
  the code without a `system`.
- Statuses, gender, patient class, marital status and interpretation use the official
  v2 tables; unmapped values fall back to `unknown` and produce a warning.
- Scheduling maps SIU to `Appointment` (SCH/AIS/AIP/AIL/AIG, participants and roles);
  documents map MDM to `DocumentReference` (TXA metadata, ED content, docStatus,
  security label, addendum/replacement relations).
- Units: OBX-6 with a code becomes a UCUM `Quantity`; a bare number without a unit is
  not a valid Quantity and is reported.
- Empty versus null: `""` becomes `dataAbsentReason`, an empty field produces no
  value element.
- FHIR -> HL7 is lossy by design. Round-trip only the fields you own and document the
  rest.

Field-by-field tables and lossiness notes: `references/fhir-mapping.md`.

## GDT bridge

`gdt_bridge.py` composes GDT -> FHIR -> HL7 and HL7 -> FHIR -> GDT. It reuses the
sibling `gdt-interface` skill for byte-exact GDT parsing and writing; pass
`--gdt-tool PATH` if that skill lives elsewhere.

- 6302 -> `ServiceRequest`, 6310 -> `DiagnosticReport` + `Observation`,
  6301 -> `Patient`.
- GDT dates are ambiguous (`TTMMJJJJ` vs `JJJJMMTT`). The script detects the format
  from a plausible year and exposes `--date-order` for a fixed convention.
- GDT field 8420 carries no unit. Without `--unit`/`--unit-code` the value is kept as
  `valueString` and a warning is emitted.
- Coded FHIR observation values are not representable in 8420; they are dropped with
  a warning instead of being written as numbers.

Mapping and pitfalls: `references/gdt-bridge.md`.

## Common pitfalls

- Reading MLLP until EOF, or searching for `\n` instead of the FS block.
- Splitting MSH-2 on the repetition character (it contains that character).
- Losing the distinction between absent and explicit null.
- Indexing fields 0-based, or forgetting that MSH-1 is the separator itself.
- Logging the raw message on validation failure.
- Acknowledging before commit, or reusing MSH-10 for a new event.
- Re-encoding with hard-coded delimiters or the wrong character set (ISO-8859-1 vs
  UTF-8 is a per-feed setting).
- Treating `AE` as success because the socket stayed open.

## Verification checklist

- `python -m unittest discover -s tests` passes.
- A stored message survives parse -> serialize -> parse unchanged.
- The ACK echoes MSH-10 and uses the swapped sender/receiver.
- An unprocessable message produces `AE`/`AR` with an ERR segment and no PHI.
- Duplicate MSH-10 does not create a second business event.
- SIU produces an `Appointment` with resolvable participant references; MDM produces a
  `DocumentReference` whose attachment decodes to the original bytes.
- Every mapped resource reference resolves inside the Bundle.
- No unit, code or identifier system is invented; gaps show up as warnings.

## References

| File | Read when |
|------|-----------|
| `references/hl7-format.md` | Delimiters, indexing, escapes, nulls, batches, encodings |
| `references/messages.md` | Message types, trigger events, required segments, field tables |
| `references/mllp-ack.md` | Framing, ACK semantics, retries, TLS, engine behaviour |
| `references/fhir-mapping.md` | HL7 <-> FHIR field mapping, code systems, lossiness |
| `references/siu-mdm.md` | SIU scheduling, MDM documents, ED content, triggers |
| `references/gdt-bridge.md` | GDT Satzarten, field mapping, German integration constraints |
| `references/python-ecosystem.md` | Libraries and engines when the stdlib approach is not enough |
| `references/production-checklist.md` | PHI, logging, DLQ, monitoring, testing, go-live |

Message examples live in `assets/`. `tests/` contains executable expectations for the
parser, mapper and GDT bridge.
