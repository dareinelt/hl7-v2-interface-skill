# GDT bridge (Germany)

GDT/xDT is a file-drop interface between German practice management systems (PVS)
and devices or modules. It is not HL7 and not a network protocol. The sibling
`gdt-interface` skill owns byte-exact parsing, validation and file handling; this
skill maps GDT records to FHIR and composes GDT <-> FHIR <-> HL7.

## Satzarten

| Satzart | Meaning | FHIR target |
|---------|---------|-------------|
| 6301 | Patient master data | `Patient` |
| 6302 | Examination request (PVS -> device) | `ServiceRequest` |
| 6310 | Examination result (device -> PVS) | `DiagnosticReport` + `Observation` |

`gdt_bridge.py` chooses the Satzart from the record for GDT input, and from the
message type for HL7 input (ADT -> 6301, ORM/OML -> 6302, ORU -> 6310). Override
with `--satzart`.

## Field mapping

| GDT field | Meaning | FHIR | Notes |
|-----------|---------|------|-------|
| 3000 | Patient number | `Patient.identifier.value` | Never join on name + birth date |
| 3101 / 3102 | Family / given name | `Patient.name` | Do not split compound names |
| 3103 | Birth date | `Patient.birthDate` | `TTMMJJJJ` or `JJJJMMTT` |
| 3105 | Insurance number | second identifier | Separate system URI |
| 3106 / 3107 | City / street | `Patient.address` | Profile-dependent |
| 3110 | Sex | `Patient.gender` | 1 male, 2 female, else unknown |
| 3618 / 3619 | Mobile / email | `Patient.telecom` | |
| 6200 | Examination date | `authoredOn` / `effectiveDateTime` | Same ambiguity as 3103 |
| 6228 | Finding text (repeats) | `DiagnosticReport.conclusion`, notes | Preserve order; choose one representation |
| 8402 | Procedure key | `code` | Local code, not LOINC |
| 8420 | Measured value (repeats) | `Observation.value[x]` | No unit in the field |
| 8315 / 8316 | Receiver / sender ID | `Device` + `Provenance` | Model as Device or Practitioner per contract |
| 9218 | GDT version | `Provenance` / metadata | Not a clinical code |

## Date ambiguity

`3103` and `6200` use different conventions across GDT 2.1 and 3.x, sometimes in the
same file. The bridge:

- Reads eight digits and ignores separators.
- Treats the first four digits as a year when they are between 1900 and 2100,
  otherwise as `DDMMYYYY`.
- Rejects impossible dates instead of coercing them.
- Exposes `--date-order ddmmyyyy|yyyymmdd` to force the contract for a feed.

Agree the convention in writing with the PVS/device vendor and pin it in configuration.

## Units

GDT field 8420 is a bare value. There is no standard unit field in the common
subset:

- Pass `--unit` and `--unit-code` (UCUM) when the device profile defines the unit.
- Without a unit the value becomes `valueString` and a warning is emitted. A
  `Quantity` without a unit is not a valid clinical result.
- Never infer a unit from the procedure code.

## Coded results

FHIR `valueCodeableConcept` has no GDT equivalent in the common field set. The bridge
drops the value with a warning instead of writing the code into 8420 (which is a
numeric measurement field). Map such results through a profile-defined field.

## Hub pattern

```text
PVS ──GDT 6302──▶ gdt_bridge ──FHIR──▶ hl7_fhir_map ──HL7 ORM──▶ KIS
KIS ──HL7 ORU───▶ hl7_fhir_map ──FHIR──▶ gdt_bridge ──GDT 6310──▶ PVS
```

Using FHIR as the canonical hub keeps one mapping per format instead of N x N
adapters. It is lossy: document which fields your integration owns and never
round-trip fields you do not need.

## File handling

Use the `gdt-interface` rules:

- Read raw bytes with the negotiated encoding (latin-1, cp1252, cp437 or UTF-8).
- Write to a temporary file in the target directory, `fsync`, then `os.replace`.
- Never process a file whose size or mtime changed within the poll interval.
- One consumer per directory; archive or quarantine, never delete evidence.
- Log field IDs and counts, not values.

## German context

- `ISiK` (Informationstechnische Systeme im Krankenhaus) is FHIR-based and does not
  replace GDT for device integration. A PVS may use GDT locally and ISiK/FHIR toward
  the hospital.
- `IHE` profiles and the gematik specifications apply to cross-organisation exchange;
  they do not remove the need to honour the local GDT profile.
- Validate FHIR resources against the target profile (ISiK, MII, or the vendor's
  profile) before sending.

## Limits

- GDT 2.1 and 3.x differ in length fields and record types; this bridge covers the
  common 6301/6302/6310 subset.
- Multiple 8420 values become multiple Observations; the link back to a specific
  procedure key is preserved only through the shared `8402`.
- The bridge does not implement the GDT test flag (`8410`) semantics; many vendors
  use it differently. Confirm with the vendor before using it as a production marker.
