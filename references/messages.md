# Message types and field tables

## Common message families

| Type | Trigger | Purpose | Minimum segments |
|------|---------|---------|------------------|
| ADT | A01 | Admit / visit notification | MSH, EVN, PID, PV1 |
| ADT | A02 | Transfer | MSH, EVN, PID, PV1 |
| ADT | A03 | Discharge | MSH, EVN, PID, PV1 |
| ADT | A04 | Outpatient registration | MSH, EVN, PID, PV1 (often without PV1) |
| ADT | A05 | Pre-admit | MSH, EVN, PID, PV1 |
| ADT | A08 | Update patient information | MSH, EVN, PID, PV1 |
| ADT | A11/A13 | Cancel admit / cancel discharge | MSH, EVN, PID, PV1 |
| ADT | A40 | Merge patient identifiers | MSH, EVN, PID, MRG |
| ORM | O01 | General order (legacy) | MSH, PID, ORC, OBR |
| OML | O21 | Laboratory order (2.5+) | MSH, PID, ORC, OBR |
| ORU | R01 | Unsolicited result | MSH, PID, OBR, OBX |
| MDM | T02 | Document notification with content | MSH, PID, TXA, OBX |
| SIU | S12-S26 | Scheduling | MSH, SCH, PID, RGS, AIS/AIG |
| BAR | P01/P02 | Account/billing | MSH, PID, EVN, PV1, DG1, GT1, IN1 |
| DFT | P03 | Detailed financial transaction | MSH, PID, FT1 |

The bundled mapper covers ADT, ORU, ORM/OML, SIU and MDM. BAR and DFT need a
dedicated mapping decision (accounting). SIU/MDM detail: `siu-mdm.md`.

## PID - patient identification

| Field | Meaning | Notes |
|-------|---------|-------|
| 1 | Set ID | Usually `1` |
| 2 | Patient ID (external) | Deprecated in favour of PID-3 |
| 3 | Patient identifier list | Repeats; CX: ID, check digit, scheme, assigning authority, type |
| 4 | Alternate patient ID | |
| 5 | Patient name | Repeats; XPN: family, given, middle, suffix, prefix |
| 6 | Mother's maiden name | |
| 7 | Date of birth | TS/DTM, may be partial |
| 8 | Sex | `M`, `F`, `O`, `U`, `A`, `N` |
| 9 | Patient alias | |
| 10 | Race | |
| 11 | Patient address | Repeats; XAD: street, other, city, state, ZIP, country |
| 12 | County code | |
| 13 | Phone - home | XTN; repeats |
| 14 | Phone - business | XTN; repeats |
| 15 | Primary language | CE |
| 16 | Marital status | CE, table 0002 |
| 29 | Death date and time | |
| 30 | Death indicator | `Y`/`N` |

PID-3 component 4 is the assigning authority (HD: namespace, universal ID, ID type).
An OID in component 4-2 is the only reliable identifier system URI.

## PV1 - patient visit

| Field | Meaning |
|-------|---------|
| 1 | Set ID |
| 2 | Patient class (table 0004): I inpatient, O outpatient, E emergency, P pre-admit |
| 3 | Assigned patient location (PL: point of care, room, bed, facility, status) |
| 7 | Attending doctor (XCN) |
| 8 | Referring doctor |
| 9 | Consulting doctor |
| 10 | Hospital service |
| 17 | Admitting doctor |
| 19 | Visit number (CX) |
| 44 | Admit date/time |
| 45 | Discharge date/time |

## ORC - common order

| Field | Meaning |
|-------|---------|
| 1 | Order control: NW new, OK/CM completed, CA cancel, HD hold, SC status changed |
| 2 | Placer order number (EI) |
| 3 | Filler order number (EI) |
| 9 | Date/time of transaction |
| 12 | Ordering provider (XCN) |
| 16 | Order control code reason (CE) |

## OBR - observation request

| Field | Meaning |
|-------|---------|
| 1 | Set ID |
| 2 | Placer order number |
| 3 | Filler order number |
| 4 | Universal service identifier (CE/CWE) - often LOINC |
| 5 | Priority: S stat, A ASAP, R routine |
| 6 | Requested date/time |
| 7 | Observation date/time |
| 16 | Ordering provider |
| 22 | Results report/status change date/time |
| 24 | Diagnostic service section ID |
| 25 | Result status: F final, P preliminary, C corrected, X cancelled |

## OBX - observation/result

| Field | Meaning | Notes |
|-------|---------|-------|
| 1 | Set ID | Sequence within the OBR block |
| 2 | Value type | NM, ST, TX, FT, CE, CWE, CNE, DT, DTM, TM, SN, NR, ED, RP |
| 3 | Observation identifier | CE/CWE, typically LOINC |
| 4 | Observation sub-ID | Distinguishes panels |
| 5 | Observation value | Interpretation depends on OBX-2 |
| 6 | Units | CE, typically UCUM |
| 7 | Reference range | Free text in most feeds |
| 8 | Abnormal flags | Repeats; table 0078 (N, A, H, L, HH, LL) |
| 11 | Result status | F, P, C, X, W, D |
| 14 | Date/time of the observation | |
| 16 | Responsible observer | XCN |
| 17 | Observation method | CE |
| 19 | Date/time of analysis | |
| 20 | Observation site | CE |
| 21 | Observation instance identifier | EI |

`OBX-5` is polymorphic. Route on `OBX-2`; never parse a value before reading its
type. `NM` without a unit in OBX-6 is a number, not a valid FHIR Quantity.

## NTE - notes and comments

| Field | Meaning |
|-------|---------|
| 1 | Set ID |
| 2 | Source of comment |
| 3 | Comment (FT, may contain `\.br\`) |
| 4 | Comment type |

NTE follows the segment it comments on (OBR, OBX, PID). `Message.notes_after(index)`
collects the immediately following block.

## MSA and ERR - acknowledgement

| Segment | Field | Meaning |
|---------|-------|---------|
| MSA | 1 | Acknowledgement code AA, AE, AR, CA, CE, CR |
| MSA | 2 | Message control ID of the acknowledged message (MSH-10) |
| MSA | 3 | Text message |
| ERR | 3 | Error code and text (CWE, table 0357) |
| ERR | 4 | Severity: W warning, E error, F fatal |

## Vendor quirks to expect

- PID-3 without assigning authority, so no identifier system can be derived.
- MSH-9 with only two components, or a non-standard structure code.
- OBX-5 populated in a CE field for a numeric observation.
- Local codes in OBR-4 with the LOINC text but no LOINC code.
- Trailing empty fields omitted entirely, or extra trailing delimiters.
- MSH-10 not unique across days - never use it alone as a permanent business key.
- Mixed line endings (`\r\n`) and a UTF-8 BOM at the start of a file.
