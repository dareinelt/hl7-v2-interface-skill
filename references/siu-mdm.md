# SIU and MDM mapping

## SIU - scheduling

### Segments

| Segment | Purpose | Key fields |
|---------|---------|------------|
| SCH | Schedule activity | 1 placer appointment ID, 2 filler appointment ID, 4 placer group number, 5 schedule ID, 6 event reason, 7 appointment reason, 8 appointment type, 9 duration, 10 duration units, 11 timing/quantity, 12 placer contact, 16 filler contact, 25 filler status, 26/27 placer/filler order number |
| TQ1 | Timing/quantity | 7 start date/time, 8 end date/time |
| RGS | Resource group | 1 set ID, 2 action code, 3 group ID |
| AIS | Service | 1 set ID, 2 action, 3 service identifier, 4 start, 7 duration, 8 units, 10 status |
| AIP | Personnel | 1 set ID, 2 action, 3 personnel (XCN, repeats), 4 role, 6 start, 12 status |
| AIL | Location | 1 set ID, 2 action, 3 location (PL), 4 location type, 6 start, 12 status |
| AIG | General resource | 1 set ID, 2 action, 3 resource ID, 4 resource type, 6 start, 14 status |

`SCH-25` and the resource status fields use table 0278 (BOOKED, PENDING,
WAITLIST, CANCELLED, BLOCKED, STARTED, COMPLETE, NOSHOW, DELETED).

### Trigger events

| Trigger | Meaning | Appointment.status |
|---------|---------|--------------------|
| S12 | New appointment | booked |
| S13 | Rescheduling | booked |
| S14 | Modification | booked |
| S15 | Cancellation | cancelled |
| S16 | Discontinuation (recurring) | cancelled |
| S17 | Deletion | entered-in-error |
| S18 | Add service/resource | booked |
| S19 | Modify service/resource | booked |
| S20 | Cancel service/resource | cancelled |
| S21 | Discontinue service/resource | cancelled |
| S22 | Delete service/resource | entered-in-error |
| S23 | Blocked slot(s) | not an appointment - no Appointment produced |
| S24 | Opened slot(s) | not an appointment - no Appointment produced |
| S25 | Schedule query (SQM/SQR) | not an SIU notification |
| S26 | Patient no-show | noshow |

`SCH-25` wins over the trigger when both are present. Unmapped codes fall back to
the trigger and produce a warning.

### Appointment mapping

| HL7 | FHIR |
|-----|------|
| SCH-1 / SCH-2 | `Appointment.identifier` with type PLAC / FILL |
| SCH-4 | `Appointment.identifier` with type text "Placer group number" |
| SCH-6 / SCH-7 | `Appointment.reasonCode` (SCH-7 first) and `description` from SCH-7 text |
| SCH-9/10, AIS-7/8 | `Appointment.minutesDuration` (minutes; h and s are converted) |
| SCH-11 / TQ1-7/8 / AIS-4 / AIG-8 / AIL-6 / AIP-6 | `Appointment.start` and `end` |
| AIS-3 | `Appointment.serviceType` (repeats) |
| AIP-3 | Participant + `Practitioner`; AIP-4 role maps to v3-ParticipationType |
| AIL-3 | Participant + `Location` (name and raw PL identifier) |
| AIG-3 | Participant + `Device` (identifier from the code, name from the text) |
| NTE | `Appointment.comment` |
| PID | `Appointment.participant` (SBJ) + `Patient` |

Participant types: `SBJ` patient, `ATND`/`REF`/`CON`/`ADM`/`PART` personnel,
`LOC` location, `PPRF` general resource. Participant status follows the appointment:
accepted, tentative (pending/waitlist), declined (cancelled), needs-action
(proposed).

### Lossiness

- SCH-8 appointment type, SCH-5 schedule ID and the offset/duration fields on
  resource segments are not mapped.
- AIL-3 is preserved as a raw PL string in `Location.identifier`; the display name is
  composed from the PL components.
- AIP-4 role codes outside the v3 participation set fall back to `PART` with a
  warning.
- S23/S24 slot operations are not modelled as `Slot` resources.

## MDM - medical documents

### Trigger events

| Trigger | Content | DocumentReference.status |
|---------|---------|--------------------------|
| T01 | No | current |
| T02 | Yes (OBX) | current |
| T03 | No | current |
| T04 | Yes | current |
| T05/T06 | No / Yes - addendum | current, `relatesTo` appends |
| T07/T08 | No / Yes - edit | current |
| T09/T10 | No / Yes - replacement | current, `relatesTo` replaces |
| T11 | No - cancel | entered-in-error |

Content-bearing triggers are T02, T04, T06, T08 and T10. A missing OBX in one of
them is a validation error.

### TXA segment

| Field | Meaning | Mapping |
|-------|---------|---------|
| 1 | Set ID | |
| 2 | Document type (table 0270) | `DocumentReference.type` |
| 3 | Content presentation (table 0191) | TXA-3 on export: TX, FT or ED |
| 4 | Activity date/time | `DocumentReference.date` |
| 5 | Primary activity provider | `DocumentReference.author` |
| 6 | Origination date/time | fallback for `date` |
| 9 | Originator | `DocumentReference.author` |
| 10 | Assigned authenticator | `DocumentReference.authenticator` |
| 12 | Unique document number (EI) | `masterIdentifier` + `identifier` |
| 13 | Parent document number | `relatesTo.target.identifier` |
| 14 / 15 | Placer / filler order number | `identifier` with type PLAC / FILL |
| 17 | Completion status (table 0271) | `docStatus` (FHIR core code; v2 code kept in `text`) |
| 18 | Confidentiality (table 0272) | `securityLabel` (v3-Confidentiality coding) |
| 19 / 20 | Availability / storage status | not mapped |
| 21 | Change reason | not mapped |
| 22 | Authentication person/time | not mapped (TXA-10 carries the person) |

`docStatus` uses the FHIR core value set: AU/LA/DO -> final, DI/IP/IN/PA ->
preliminary. The original v2 code (for example `AU Authenticated`) is kept in
`docStatus.text` so a round-trip can restore TXA-17. T11 sets
`DocumentReference.status = entered-in-error`.

### Document content (OBX-2 = ED)

| ED component | Meaning | Attachment field |
|--------------|---------|------------------|
| 1 | Source application | not mapped |
| 2 | Type of data (Application, Image, Text) | used with component 3 |
| 3 | Data subtype (PDF, TEXT, RTF, HTML, JPEG, ...) | `contentType` |
| 4 | Encoding (Base64, Hex, A) | decoded, re-encoded as base64 |
| 5 | Data | `Attachment.data` |

- Base64 data is validated and re-encoded canonically.
- Hex and ASCII data are decoded to bytes, then base64-encoded.
- Unknown subtypes become `application/octet-stream` with a warning.
- `Attachment.size` is the decoded byte length; `creation` comes from OBX-14.
- Multiple ED OBX segments become multiple `content` entries.
- `NTE` after TXA is stored in `DocumentReference.description` (the only agreed local
  slot) and re-emitted as NTE on export; without NTE the description carries the
  document type text.

### Lossiness

- TXA-19/TXA-20 availability and storage status, TXA-21 change reason and TXA-22
  authentication timestamps are not represented.
- Multiple ED OBX segments become multiple `content` entries on import; export uses the
  first attachment only.
- A document that exceeds the MLLP size limit must be retrieved out of band; the
  mapper does not stream attachments.
- Binary content in `DocumentReference.content.attachment.data` is base64 in JSON;
  database and transport size limits still apply.
- `docStatus` uses the FHIR core status code, not the v2 table code. The v2 code lives
  in `docStatus.text`; consumers that need it must read the text or re-map.
- TXA-18 is mapped to v3-Confidentiality when the code is N, R, V or U; other codes
  are passed through unchanged.
