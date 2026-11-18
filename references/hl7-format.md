# HL7 v2 ER7 format

## Message shape

An HL7 v2 message is a sequence of segments separated by carriage return (`\r`,
0x0D). Each segment is one line:

```text
MSH|^~\&|SENDING_APP|SENDING_FAC|RECEIVING_APP|RECEIVING_FAC|20260314153000||ADT^A01^ADT_A01|MSG00001|P|2.5.1
PID|1||123456^^^HOSP&1.2.3.4&ISO^MR||DOE^JANE^A||19820411|F
```

- The first three characters are the segment name. `Z` segments are locally defined.
- Fields are separated by the character declared in MSH-1.
- Components, repetitions, escapes and subcomponents are declared in MSH-2 in this
  order: component, repetition, escape, subcomponent. Defaults: `^`, `~`, `\`, `&`.

## Indexing rules

| Type | Index base | Notes |
|------|------------|-------|
| Segment | 1 | `PID` is one segment |
| Field | 1 | `PID-5` is the fifth field |
| Component | 1 | `PID-5-1` is the family name |
| Subcomponent | 1 | `PID-3-4-1` is the namespace of the assigning authority |
| Repetition | 1 | `PID-3[2]` is the second identifier |

MSH is special: **MSH-1 is the field separator itself**, and MSH-2 is the encoding
characters. There is no field before MSH-1; the `|` after `MSH` is MSH-1, not a
delimiter that precedes it. `Segment.fields[0]` is therefore MSH-1 for MSH and
PID-1 for PID.

## Empty versus explicit null

- `PID|1|||DOE^JANE` - PID-3 is empty: **not sent**.
- `PID|1||""|DOE^JANE` - PID-3 contains `""`: **explicitly null** ("delete the
  previous value").

Both must survive a round-trip. The bundled parser reports `is_null(field)` for the
second case and never turns one into the other.

## Escape sequences

| Sequence | Meaning |
|----------|---------|
| `\F\` | Field separator |
| `\S\` | Component separator |
| `\R\` | Repetition separator |
| `\T\` | Subcomponent separator |
| `\E\` | Escape character |
| `\Xdd..\` | Hex bytes |
| `\.br\` | Line break |
| `\Z..\` | Locally defined |

Escape the escape character first, then the separators. Decode on read; encode on
write. Never split a field on a separator before unescaping - a literal `^` inside a
name is `\S\`, not a component boundary.

## Repetitions

A field may repeat: `PID-3` with two identifiers is
`123456^^^HOSP^MR~987654^^^LAB^PI`. Repeating fields are common for identifiers,
names, addresses, phone numbers, allergy codes and OBX abnormal flags. Preserve the
order; never collapse repetitions into one value unless the target schema forces it.

## Character encoding

HL7 v2 does not mandate UTF-8. Common feeds are ISO-8859-1 (latin-1), Windows-1252
or UTF-8, and some vendors send mixed encodings across interfaces.

- Make the encoding a per-feed configuration value.
- Do not guess silently; log the detected/configured encoding per sender.
- Preserve the original bytes for audit when a message is rejected.
- `Message.parse(bytes)` tries UTF-8 and falls back to latin-1; pass `encoding=` when
  the contract is known.

## Batches

A batch may be wrapped in `FHS` (file header) and `BHS` (batch header) segments and
contain several messages, each starting with `MSH`.

- `Message.parse_batch()` splits on MSH and ignores FHS/BHS.
- Preserve the batch structure if the receiver expects it; otherwise process messages
  individually.
- Do not assume one message per TCP connection or one file per message.

## Versions

v2.3, 2.3.1, 2.4, 2.5, 2.5.1, 2.6, 2.7, 2.8 and 2.8.2 are in active use. Field
positions for the segments in this skill are stable across these versions; message
structures and trigger events change. Read MSH-12 and validate against that version,
not against "the latest standard".

## Z-segments

Vendors add `Z` segments for local data (e.g. `ZDS`, `ZVN`, `ZPI`). They are part of
the interface contract:

- Preserve them on pass-through.
- Do not map them to FHIR without an agreed mapping.
- Prefer tolerating an unknown Z-segment over rejecting the message; reject only when
  it carries data the receiver must act on.
