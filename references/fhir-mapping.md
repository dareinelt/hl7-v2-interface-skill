# HL7 v2 to FHIR R4 mapping

## Bundle layout

The mapper emits a `Bundle` of type `collection` by default:

| HL7 input | FHIR resources |
|-----------|----------------|
| ADT^A01/A02/A03/A04/A05/A08 | Patient, Encounter, Practitioner (per participant) |
| ORU^R01 | Patient, ServiceRequest (when ORC exists), DiagnosticReport, Observation, Practitioner |
| ORM^O01 / OML^O21 | Patient, ServiceRequest, Practitioner |
| SIU^S12-S26 | Patient, Appointment, Practitioner, Location, Device |
| MDM^T01-T11 | Patient, Encounter (when PV1 exists), DocumentReference, Practitioner |

Details for scheduling and documents: `siu-mdm.md`.

`--bundle-type transaction` adds a `request` to every entry. Patient uses a
conditional update by identifier; other resources use POST. Review this against your
server's idempotency rules before using it in production.

`fullUrl` values are deterministic (`uuid5`) when a message control ID is present, so
replaying the same message produces the same references. `--random-uuids` disables
that for tests.

## ADT -> Patient

| HL7 | FHIR | Notes |
|-----|------|-------|
| PID-3 repetitions | `Patient.identifier` | CX -> Identifier; component 4 is the assigning authority |
| PID-3-5 | `identifier.type` | Table 0203: MR, SS, PI, DL, PPN, PRN, VN |
| PID-5 | `Patient.name` | XPN -> HumanName; first repetition official, later ones usual |
| PID-7 | `Patient.birthDate` | Partial dates stay partial |
| PID-8 | `Patient.gender` | M/F/O/U/A/N -> male/female/other/unknown |
| PID-11 | `Patient.address` | XAD -> Address; components 1-6 |
| PID-13/14 | `Patient.telecom` | XTN -> ContactPoint; email when equipment is Internet |
| PID-15 | `Patient.communication.language` | CE -> CodeableConcept |
| PID-16 | `Patient.maritalStatus` | Table 0002 -> v3-MaritalStatus |
| PID-29/30 | `deceasedDateTime` / `deceasedBoolean` | |

## ADT -> Encounter

| HL7 | FHIR | Notes |
|-----|------|-------|
| MSH-9 trigger | `Encounter.status` | A01/A02/A04/A08 in-progress, A03 finished, A05 planned |
| PV1-2 | `Encounter.class` | I->IMP, O->AMB, E->EMER, P->PRENC; unknown falls back to AMB with a warning |
| PV1-3 | `Encounter.location.location.display` | PL components composed; not a Location resource |
| PV1-7/8/9/17 | `Encounter.participant` | ATND/REF/CON/ADM + Practitioner resource |
| PV1-19 | `Encounter.identifier` | CX -> Identifier |
| PV1-44/45 | `Encounter.period` | EVN-2 is the fallback start |
| PV1-10 | `Encounter.serviceType.text` | |

## ORU -> DiagnosticReport

| HL7 | FHIR | Notes |
|-----|------|-------|
| OBR-3 / OBR-2 | `DiagnosticReport.identifier` | Filler first, placer second |
| OBR-4 | `DiagnosticReport.code` | CE/CWE -> CodeableConcept |
| OBR-7 / OBR-22 | `effectiveDateTime` / `issued` | |
| OBR-16 | `DiagnosticReport.performer` | XCN -> Practitioner reference |
| OBR-25 | `DiagnosticReport.status` | F final, P preliminary, C corrected, X cancelled |
| OBR-26..28 | not mapped | |
| ORC (if present) | `DiagnosticReport.basedOn` | References the ServiceRequest |
| NTE after OBR | `DiagnosticReport.note` | |

## ORU -> Observation

| HL7 | FHIR | Notes |
|-----|------|-------|
| OBX-2 | `value[x]` selector | See value table below |
| OBX-3 | `Observation.code` | LOINC when component 3 is `LN` |
| OBX-5 | `value[x]` | Polymorphic |
| OBX-6 | `valueQuantity` unit | UCUM when a code exists |
| OBX-7 | `referenceRange.text` | Free text, not a structured range |
| OBX-8 | `interpretation` | Table 0078 -> v2-0078 coding |
| OBX-11 | `Observation.status` | F/P/C/X/W/D |
| OBX-14 / OBX-19 | `effectiveDateTime` / `issued` | |
| OBX-16 | `Observation.performer` | XCN -> Practitioner |
| OBX-17 | `Observation.method` | CE -> CodeableConcept |
| OBX-20 | `Observation.bodySite` | CE -> CodeableConcept |
| OBX-21 | `Observation.identifier` | EI -> Identifier |
| NTE after OBX | `Observation.note` | |

`Observation.category` is set to `laboratory` (v2-0074 `LAB`) for ORU results.

### OBX-2 value types

| OBX-2 | FHIR element | Constraint |
|-------|--------------|------------|
| NM, SN | `valueQuantity` | Non-numeric SN falls back to `valueString` |
| ST, TX, FT | `valueString` | |
| CE, CWE, CNE | `valueCodeableConcept` | |
| DT, DTM, TS | `valueDateTime` | Partial dates allowed |
| TM | `valueTime` | `HH:MM[:SS]` |
| NR | `valueRange` | Only when two numeric bounds are present |
| ED, RP | `valueString` | Binary content is not converted; warned |
| BO-like `Y`/`N` | `valueString` | |
| empty | `valueString` | Warned |

Explicit null `""` becomes `dataAbsentReason: {text: "unknown"}`. An empty OBX-5
becomes `dataAbsentReason: {text: "not-performed"}`.

## ORM/OML -> ServiceRequest

| HL7 | FHIR | Notes |
|-----|------|-------|
| ORC-1 | `ServiceRequest.status` | NW active, CM/OK completed, CA revoked, HD on-hold |
| ORC-2 / ORC-3 | `ServiceRequest.identifier` | Placer and filler |
| ORC-9 | `authoredOn` | |
| ORC-12 | `requester` | XCN -> Practitioner |
| ORC-16 | `reasonCode` | CE -> CodeableConcept |
| OBR-4 | `code` | |
| OBR-5 | `priority` | S stat, A urgent, R routine |
| OBR-6/7 | `occurrenceDateTime` | |
| OBR-16 | performer fallback | |

`ServiceRequest.intent` is `order`. The mapper does not create MedicationRequest,
Procedure or DiagnosticRequest subclasses.

## SIU -> Appointment

| HL7 | FHIR | Notes |
|-----|------|-------|
| SCH-1 / SCH-2 | `Appointment.identifier` | Types PLAC and FILL |
| SCH-4 | `Appointment.identifier` | Type text "Placer group number" |
| SCH-6 / SCH-7 | `reasonCode`, `description` | SCH-7 first |
| SCH-9/10, AIS-7/8 | `minutesDuration` | Converted to minutes |
| SCH-11, TQ1-7/8, AIS-4, AIG-8, AIL-6, AIP-6 | `start`, `end` | First usable value wins |
| AIS-3 | `serviceType` | Repeats |
| AIP-3 / AIP-4 | Participant + Practitioner | Role becomes v3-ParticipationType |
| AIL-3 | Participant + Location | Raw PL kept as identifier |
| AIG-3 | Participant + Device | Code kept as identifier |
| SCH-25 or trigger | `Appointment.status` | booked, pending, waitlist, cancelled, noshow, entered-in-error |
| NTE | `Appointment.comment` | |

## MDM -> DocumentReference

| HL7 | FHIR | Notes |
|-----|------|-------|
| TXA-12 | `masterIdentifier` + `identifier` | EI |
| TXA-14 / TXA-15 | `identifier` | Types PLAC and FILL |
| TXA-2 | `DocumentReference.type` | CodeableConcept |
| TXA-4 / TXA-6 | `date` | |
| TXA-9 / TXA-5 | `author` | Practitioners |
| TXA-10 | `authenticator` | Practitioner |
| TXA-17 | `docStatus` | AU/LA/DO final, DI/IP/IN/PA preliminary; v2 code in `docStatus.text` |
| TXA-18 | `securityLabel` | Table 0272 -> v3-Confidentiality |
| TXA-13 + trigger | `relatesTo` | appends (T05/T06) or replaces (T09/T10) |
| OBX-2 ED | `content.attachment` | Base64 validated; MIME from ED-3 |
| TXA-11 (trigger) | `status` | T11 -> entered-in-error |
| NTE | `description` | Local slot for document comments |

## Identifier systems

| HL7 assigning authority | FHIR `identifier.system` |
|-------------------------|--------------------------|
| OID in component 2 | `urn:oid:1.2.3.4` |
| UUID with type `UUID` | `urn:uuid:...` |
| Namespace only | `urn:hl7:facility:<slug>` (local) |
| Nothing | omitted, or `--patient-system` |

Never invent an OID. Agree the system URI with the receiving system and keep it in
configuration.

## Code systems

| HL7 table code | FHIR system |
|----------------|-------------|
| LN / LOINC | `http://loinc.org` |
| SCT / SNM | `http://snomed.info/sct` |
| I10 | `http://hl7.org/fhir/sid/icd-10` |
| I9 | `http://hl7.org/fhir/sid/icd-9-cm` |
| UCUM | `http://unitsofmeasure.org` |
| Unknown/local | coding without `system` |

## FHIR -> HL7

- `Patient` -> PID, `Encounter` -> PV1, `Observation` -> OBX,
  `DiagnosticReport` -> OBR, `ServiceRequest` -> ORC + OBR,
  `Appointment` -> SCH + TQ1 + RGS + AIS/AIP/AIL/AIG,
  `DocumentReference` -> TXA + OBX (ED).
- Message type is inferred (DocumentReference -> MDM, Appointment -> SIU,
  Observation/DiagnosticReport -> ORU, ServiceRequest -> ORM, otherwise ADT^A08) or
  passed explicitly with `--type`.
- MSH-9 is built as `CODE^TRIGGER^CODE_TRIGGER`.
- Identifiers reverse: `urn:oid:` -> HD with universal ID and type ISO, `urn:uuid:` ->
  UUID, other URIs -> a slugged namespace.
- Codes reverse to CE/CWE components: code, text, table code.
- Quantities reverse to OBX-2 `NM`, OBX-5 value and OBX-6 units.

## Known lossiness

| Lost or approximated | Reason |
|----------------------|--------|
| PV1-3 location structure | Stored as a display string; compressing it back is ambiguous |
| ORC-1 control codes | FHIR status has fewer states; RE/SC become active |
| OBX-7 reference range | Free text, no low/high/unit parsing |
| ED/RP in ORU results | Kept as text; only MDM document content is decoded |
| Z-segments | Not mapped |
| NK1, AL1, DG1, IN1, GT1 | Not mapped |
| SIU SCH-5/8, offset fields | Not mapped |
| SIU S23/S24 slot operations | Not modelled as `Slot` |
| MDM TXA-19/20/21/22 | Availability, storage, change reason and authentication time are not represented |
| Document attachments | Base64 in JSON; no streaming for very large documents |
| OBX sub-ID / panels | Not represented as `hasMember` |
| Original delimiters | Replaced by the configured separators |
