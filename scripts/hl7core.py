#!/usr/bin/env python3
"""HL7 v2.x ER7 core: parsing, serialization, validation, ACK and MLLP.

Standard library only. Designed for Python 3.10+.

The parser keeps the exact ER7 text of every field. It never rewrites a
message that it only reads: `Message.to_er7()` reproduces the input except
for trailing empty delimiter runs. Escape sequences, explicit nulls (""),
repetitions, components and subcomponents are preserved.
"""

from __future__ import annotations

import re
import socket
import ssl
import uuid
from dataclasses import dataclass, field as dc_field
from datetime import datetime
from typing import Any, Sequence

__all__ = [
    "CR",
    "FS",
    "VT",
    "Hl7Error",
    "Issue",
    "Message",
    "Segment",
    "Separators",
    "build_ack",
    "build_minimal_ack",
    "escape_text",
    "mllp_frame",
    "mllp_unframe",
    "parse_path",
    "send_mllp",
    "strip_mllp",
    "unescape_text",
    "validate",
]

VT = b"\x0b"  # MLLP start block
FS = b"\x1c"  # MLLP end block
CR = b"\r"

SEGMENT_NAME_RE = re.compile(r"^[A-Z][A-Z0-9]{2}$")
PATH_RE = re.compile(r"^(?P<seg>[A-Z][A-Z0-9]{2})-(?P<field>\d+)(?:-(?P<comp>\d+))?(?:\[(?P<rep>\d+)\])?$")
VERSION_RE = re.compile(r"^2\.\d(?:\.\d)?$")

SIMPLE_ESCAPES = {"F": "field", "S": "component", "R": "repetition", "T": "subcomponent", "E": "escape"}

PROCESSING_IDS = {"P", "T", "D"}
ACK_CODES = {"AA", "AE", "AR", "CA", "CE", "CR"}
SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2}


class Hl7Error(ValueError):
    """Raised when a message cannot be parsed or constructed safely."""


# --------------------------------------------------------------------------
# Escape handling
# --------------------------------------------------------------------------

def escape_text(value: str, separators: "Separators", *, line_breaks: bool = False) -> str:
    """Escape a plain text value for use inside an ER7 field.

    The escape character is replaced first so that the inserted escape
    sequences are not escaped a second time.
    """
    if value is None:
        value = ""
    text = value.replace(separators.escape, separators.escape + "E" + separators.escape)
    text = text.replace(separators.field, separators.escape + "F" + separators.escape)
    text = text.replace(separators.component, separators.escape + "S" + separators.escape)
    text = text.replace(separators.repetition, separators.escape + "R" + separators.escape)
    text = text.replace(separators.subcomponent, separators.escape + "T" + separators.escape)
    if line_breaks:
        text = text.replace("\r\n", separators.escape + ".br" + separators.escape)
        text = text.replace("\r", separators.escape + ".br" + separators.escape)
        text = text.replace("\n", separators.escape + ".br" + separators.escape)
    return text


def unescape_text(value: str, separators: "Separators") -> str:
    """Decode ER7 escape sequences. Unknown sequences are kept verbatim."""
    if not value or separators.escape not in value:
        return value

    pattern = re.compile(
        re.escape(separators.escape)
        + r"(F|S|R|T|E|X[0-9A-Fa-f]*|C[0-9A-Fa-f]*|\.[A-Za-z0-9]+)"
        + re.escape(separators.escape)
    )

    def replace(match: re.Match[str]) -> str:
        code = match.group(1)
        if code in SIMPLE_ESCAPES:
            return getattr(separators, SIMPLE_ESCAPES[code])
        if code == ".br":
            return "\n"
        if code.startswith("X") and len(code) > 1:
            try:
                return bytes.fromhex(code[1:]).decode("utf-8", "replace")
            except ValueError:
                return match.group(0)
        return match.group(0)

    return pattern.sub(replace, value)


# --------------------------------------------------------------------------
# Separators / delimiters
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Separators:
    field: str = "|"
    component: str = "^"
    repetition: str = "~"
    escape: str = "\\"
    subcomponent: str = "&"

    def __post_init__(self) -> None:
        chars = [self.field, self.component, self.repetition, self.escape, self.subcomponent]
        if any(len(char) != 1 for char in chars):
            raise Hl7Error("separators must be single characters")
        if any(ord(char) < 0x20 for char in chars):
            raise Hl7Error("separators must not be control characters")
        if len(set(chars)) != 5:
            raise Hl7Error("separators must be distinct")

    @property
    def encoding_characters(self) -> str:
        """MSH-2: component, repetition, escape, subcomponent."""
        return self.component + self.repetition + self.escape + self.subcomponent

    @classmethod
    def from_msh(cls, line: str) -> "Separators":
        if not line.startswith("MSH"):
            raise Hl7Error("MSH segment expected")
        if len(line) < 8:
            raise Hl7Error("MSH segment is too short to declare separators")
        field_sep = line[3]
        encoding = line[4:8]
        return cls(field_sep, encoding[0], encoding[1], encoding[2], encoding[3])


# --------------------------------------------------------------------------
# Segments
# --------------------------------------------------------------------------

@dataclass
class Segment:
    """One ER7 segment.

    `fields[i]` is field `i + 1`. For MSH, field 1 is the field separator and
    field 2 the encoding characters, matching the HL7 definition.
    """

    name: str
    fields: list[str] = dc_field(default_factory=list)
    separators: Separators = dc_field(default_factory=Separators)

    def __post_init__(self) -> None:
        if not SEGMENT_NAME_RE.match(self.name):
            raise Hl7Error(f"invalid segment name: {self.name!r}")

    # -- read -------------------------------------------------------------
    def field(self, number: int, default: str = "") -> str:
        if number < 1:
            raise IndexError("field numbers are 1-based")
        return self.fields[number - 1] if number <= len(self.fields) else default

    def repetitions(self, number: int) -> list[str]:
        raw = self.field(number)
        # MSH-1 and MSH-2 are delimiters, not structured values. MSH-2 contains
        # the repetition character itself, so it must not be split.
        if self.name == "MSH" and number <= 2:
            return [raw] if raw != "" else []
        return raw.split(self.separators.repetition) if raw != "" else []

    def components(self, number: int, rep: int = 1) -> list[str]:
        reps = self.repetitions(number)
        if rep < 1 or rep > len(reps):
            return []
        if self.name == "MSH" and number <= 2:
            return reps
        return reps[rep - 1].split(self.separators.component)

    def subcomponents(self, number: int, comp: int = 1, rep: int = 1) -> list[str]:
        comps = self.components(number, rep)
        if comp < 1 or comp > len(comps):
            return []
        return comps[comp - 1].split(self.separators.subcomponent)

    def get(
        self,
        number: int,
        comp: int | None = None,
        sub: int | None = None,
        rep: int = 1,
        default: str = "",
    ) -> str:
        if comp is None:
            reps = self.repetitions(number)
            return reps[rep - 1] if 0 < rep <= len(reps) else default
        comps = self.components(number, rep)
        if comp < 1 or comp > len(comps):
            return default
        if sub is None:
            return comps[comp - 1]
        subs = comps[comp - 1].split(self.separators.subcomponent)
        return subs[sub - 1] if 0 < sub <= len(subs) else default

    def get_text(self, number: int, comp: int | None = None, rep: int = 1, default: str = "") -> str:
        return unescape_text(self.get(number, comp=comp, rep=rep, default=default).strip(), self.separators)

    def is_null(self, number: int) -> bool:
        """HL7 explicit null (two double quotes) versus an absent field."""
        return self.field(number) == '""'

    # -- write ------------------------------------------------------------
    def set_field(self, number: int, raw: str) -> None:
        """Set a field to already-encoded ER7 text."""
        if number < 1:
            raise IndexError("field numbers are 1-based")
        while len(self.fields) < number:
            self.fields.append("")
        self.fields[number - 1] = raw

    def set_value(self, number: int, value: str, comp: int = 1, rep: int = 1) -> None:
        """Set one component of one repetition from plain text (escaped here)."""
        encoded = escape_text(value, self.separators)
        reps = self.repetitions(number)
        if not reps:
            reps = [""]
        while len(reps) < rep:
            reps.append("")
        comps = reps[rep - 1].split(self.separators.component) if reps[rep - 1] else [""]
        while len(comps) < comp:
            comps.append("")
        comps[comp - 1] = encoded
        reps[rep - 1] = self.separators.component.join(comps)
        self.set_field(number, self.separators.repetition.join(reps))


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------

class Message:
    """An HL7 v2 message (one MSH and its segments)."""

    def __init__(self, segments: Sequence[Segment], separators: Separators, encoding: str = "utf-8") -> None:
        if not segments:
            raise Hl7Error("a message needs at least one segment")
        if segments[0].name != "MSH":
            raise Hl7Error("the first segment must be MSH")
        self.segments = list(segments)
        self.separators = separators
        self.encoding = encoding
        self.warnings: list[str] = []

    # -- construction -----------------------------------------------------
    @classmethod
    def parse(
        cls,
        data: bytes | str,
        *,
        encoding: str | None = None,
        strict: bool = True,
    ) -> "Message":
        text, used_encoding = _decode(data, encoding)
        text = _strip_mllp_text(text).lstrip("\ufeff")
        lines = [line for line in re.split(r"\r\n|\r|\n", text) if line != ""]
        if not lines:
            raise Hl7Error("message is empty")
        if lines[0].startswith(("FHS", "BHS")):
            lines = _drop_batch_headers(lines)
            if not lines:
                raise Hl7Error("batch contains no MSH segment")
        return cls._from_lines(lines, used_encoding, strict=strict)

    @classmethod
    def parse_batch(
        cls,
        data: bytes | str,
        *,
        encoding: str | None = None,
        strict: bool = False,
    ) -> list["Message"]:
        """Split a batch (optionally wrapped in FHS/BHS) into messages."""
        text, used_encoding = _decode(data, encoding)
        text = _strip_mllp_text(text).lstrip("\ufeff")
        lines = [line for line in re.split(r"\r\n|\r|\n", text) if line != ""]
        chunks: list[list[str]] = []
        current: list[str] = []
        for line in lines:
            if line.startswith("MSH"):
                if current:
                    chunks.append(current)
                current = [line]
            elif current:
                current.append(line)
        if current:
            chunks.append(current)
        if not chunks:
            raise Hl7Error("batch contains no MSH segment")
        return [cls._from_lines(chunk, used_encoding, strict=strict) for chunk in chunks]

    @classmethod
    def _from_lines(cls, lines: list[str], encoding: str, *, strict: bool) -> "Message":
        separators = Separators.from_msh(lines[0])
        segments: list[Segment] = []
        for index, line in enumerate(lines):
            name = line[:3]
            if not SEGMENT_NAME_RE.match(name):
                if strict:
                    raise Hl7Error(f"invalid segment name in line {index + 1}: {name!r}")
                continue
            if name == "MSH":
                if index != 0:
                    raise Hl7Error("MSH must be the first segment")
                parts = line.split(separators.field)
                if len(parts) < 2:
                    raise Hl7Error("MSH has no encoding characters")
                fields = [separators.field, parts[1]] + parts[2:]
            else:
                parts = line.split(separators.field)
                fields = parts[1:]
            segments.append(Segment(name, _trim_trailing_empty(fields), separators))
        return cls(segments, separators, encoding)

    # -- serialization ----------------------------------------------------
    def to_er7(self, *, trim_trailing_fields: bool = True) -> str:
        lines: list[str] = []
        for segment in self.segments:
            fields = list(segment.fields)
            if trim_trailing_fields:
                fields = _trim_trailing_empty(fields)
            if segment.name == "MSH":
                lines.append("MSH" + self.separators.field + self.separators.field.join(fields[1:]))
            else:
                lines.append(segment.name + self.separators.field + self.separators.field.join(fields))
        return "\r".join(lines) + "\r"

    def to_bytes(self, encoding: str | None = None) -> bytes:
        return self.to_er7().encode(encoding or self.encoding)

    def mllp_bytes(self, encoding: str | None = None) -> bytes:
        return mllp_frame(self.to_bytes(encoding))

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Message {self.message_type[0]}^{self.message_type[1]} id={self.control_id!r} segments={len(self.segments)}>"

    # -- access -----------------------------------------------------------
    @property
    def message_type(self) -> tuple[str, str, str]:
        parts = self.get("MSH-9").split(self.separators.component)
        code = unescape_text(parts[0], self.separators) if parts else ""
        trigger = unescape_text(parts[1], self.separators) if len(parts) > 1 else ""
        structure = unescape_text(parts[2], self.separators) if len(parts) > 2 else ""
        return code, trigger, structure

    @property
    def control_id(self) -> str:
        return unescape_text(self.get("MSH-10"), self.separators)

    @property
    def version(self) -> str:
        return unescape_text(self.get("MSH-12"), self.separators)

    @property
    def processing_id(self) -> str:
        return unescape_text(self.get("MSH-11"), self.separators)

    def segment(self, name: str, occurrence: int = 1) -> Segment | None:
        if occurrence < 1:
            return None
        seen = 0
        for item in self.segments:
            if item.name == name:
                seen += 1
                if seen == occurrence:
                    return item
        return None

    def segments_named(self, name: str) -> list[Segment]:
        return [item for item in self.segments if item.name == name]

    def require_segment(self, name: str, occurrence: int = 1) -> Segment:
        found = self.segment(name, occurrence)
        if found is None:
            raise Hl7Error(f"segment {name} ({occurrence}) not found")
        return found

    def index_of(self, name: str, occurrence: int = 1) -> int:
        if occurrence < 1:
            raise Hl7Error("occurrence is 1-based")
        seen = 0
        for index, item in enumerate(self.segments):
            if item.name == name:
                seen += 1
                if seen == occurrence:
                    return index
        raise Hl7Error(f"segment {name} ({occurrence}) not found")

    def insert(self, index: int, segment: Segment) -> None:
        if segment.separators != self.separators:
            segment = Segment(segment.name, list(segment.fields), self.separators)
        self.segments.insert(index, segment)

    def append(self, segment: Segment) -> None:
        self.insert(len(self.segments), segment)

    def get(self, path: str, default: str = "") -> str:
        seg_name, number, comp, rep = parse_path(path)
        segment = self.segment(seg_name)
        if segment is None:
            return default
        return segment.get(number, comp=comp, rep=rep, default=default)

    def get_text(self, path: str, default: str = "") -> str:
        return unescape_text(self.get(path, default=default).strip(), self.separators)

    def get_all(self, path: str) -> list[str]:
        """All repetitions of a field, optionally reduced to one component."""
        seg_name, number, comp, _rep = parse_path(path)
        segment = self.segment(seg_name)
        if segment is None:
            return []
        values: list[str] = []
        for rep in range(1, len(segment.repetitions(number)) + 1):
            if comp is None:
                values.append(segment.get(number, rep=rep))
            else:
                comps = segment.components(number, rep)
                if 0 < comp <= len(comps):
                    values.append(comps[comp - 1])
        return values

    def set(self, path: str, value: str) -> None:
        """Set a path.

        With a component (`PID-5-1`) the value is plain text and escaped.
        Without a component (`PID-5`) the value is raw ER7 and inserted as is.
        """
        seg_name, number, comp, rep = parse_path(path)
        segment = self.segment(seg_name)
        if segment is None:
            raise Hl7Error(f"segment {seg_name} not found")
        if segment.name == "MSH" and number <= 2:
            raise Hl7Error("MSH-1 and MSH-2 define the delimiters and cannot be set")
        if comp is None:
            segment.set_field(number, value)
        else:
            segment.set_value(number, value, comp=comp, rep=rep)

    # -- convenience ------------------------------------------------------
    def notes_after(self, index: int) -> list[str]:
        """NTE-3 texts directly following a segment."""
        notes: list[str] = []
        for segment in self.segments[index + 1:]:
            if segment.name != "NTE":
                break
            text = segment.get_text(3)
            if text:
                notes.append(text)
        return notes

    def clone(self) -> "Message":
        clone = Message(
            [Segment(item.name, list(item.fields), self.separators) for item in self.segments],
            self.separators,
            self.encoding,
        )
        return clone

    def to_dict(self) -> dict[str, Any]:
        return {
            "encoding": self.encoding,
            "separators": {
                "field": self.separators.field,
                "component": self.separators.component,
                "repetition": self.separators.repetition,
                "escape": self.separators.escape,
                "subcomponent": self.separators.subcomponent,
            },
            "message_type": list(self.message_type),
            "control_id": self.control_id,
            "version": self.version,
            "processing_id": self.processing_id,
            "segments": [{"name": item.name, "fields": list(item.fields)} for item in self.segments],
        }


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

def parse_path(path: str) -> tuple[str, int, int | None, int]:
    match = PATH_RE.match(path.strip())
    if not match:
        raise Hl7Error(f"invalid path {path!r}; expected SEG-FIELD[-COMPONENT][REPETITION], e.g. PID-5-1")
    return (
        match.group("seg"),
        int(match.group("field")),
        int(match.group("comp")) if match.group("comp") else None,
        int(match.group("rep") or 1),
    )


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

@dataclass
class Issue:
    severity: str
    code: str
    message: str
    segment: str | None = None
    field: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "segment": self.segment,
            "field": self.field,
        }

    def __str__(self) -> str:
        location = self.segment or "message"
        if self.field:
            location += f"-{self.field}"
        return f"{self.severity.upper():<7} {self.code:<8} {location:<10} {self.message}"


def _issue(severity: str, code: str, message: str, segment: str | None = None, field: int | None = None) -> Issue:
    return Issue(severity, code, message, segment, field)


def validate(message: Message, *, strict: bool = False) -> list[Issue]:
    """Structural and message-type specific checks.

    This is not a conformance-profile validator. It catches the defects that
    break interfaces in practice; profile validation belongs to HAPI, hl7apy
    or the interface engine.
    """
    issues: list[Issue] = []
    msh = message.segment("MSH")

    if msh is None:
        return [_issue("error", "MSH-001", "message has no MSH segment")]

    if msh.separators.field not in {"|", "^", "~", "\\", "&"}:
        issues.append(_issue("warning", "MSH-002", f"unusual field separator {msh.separators.field!r}", "MSH", 1))
    if len(set(msh.separators.encoding_characters)) != 4:
        issues.append(_issue("error", "MSH-003", "MSH-2 encoding characters are not four distinct characters", "MSH", 2))

    type_parts = msh.get(9).split(msh.separators.component)
    if not msh.get(9):
        issues.append(_issue("error", "MSH-004", "MSH-9 message type is missing", "MSH", 9))
    elif len(type_parts) < 2:
        issues.append(_issue("warning", "MSH-004", "MSH-9 has no trigger event (expected CODE^TRIGGER)", "MSH", 9))

    if not msh.get(10):
        issues.append(_issue("warning", "MSH-005", "MSH-10 message control ID is missing; idempotency is impossible", "MSH", 10))
    if msh.get(11) and msh.get(11) not in PROCESSING_IDS:
        issues.append(_issue("warning", "MSH-006", f"MSH-11 processing ID {msh.get(11)!r} is not P, T or D", "MSH", 11))
    if not msh.get(12):
        issues.append(_issue("error", "MSH-007", "MSH-12 version is missing", "MSH", 12))
    elif not VERSION_RE.match(msh.get(12)):
        issues.append(_issue("warning", "MSH-007", f"unusual HL7 version {msh.get(12)!r}", "MSH", 12))

    code = message.message_type[0]
    trigger = message.message_type[1]

    if code in {"ADT", "ORU", "ORM", "OML", "SIU", "MDM"} and message.segment("PID") is None:
        issues.append(_issue("error", "PID-001", f"{code} message has no PID segment", "PID"))

    pid = message.segment("PID")
    if pid is not None:
        if not pid.get(3):
            issues.append(_issue("error", "PID-002", "PID-3 patient identifier is missing", "PID", 3))
        if not pid.get(5):
            issues.append(_issue("warning", "PID-003", "PID-5 patient name is missing", "PID", 5))

    if code == "ADT" and trigger in {"A01", "A02", "A03"} and message.segment("PV1") is None:
        issues.append(_issue("error", "PV1-001", f"ADT^A{trigger[1:]} has no PV1 segment", "PV1"))

    if code == "ORU":
        if message.segment("OBR") is None:
            issues.append(_issue("error", "ORU-001", "ORU message has no OBR segment", "OBR"))
        obx_segments = message.segments_named("OBX")
        if not obx_segments:
            issues.append(_issue("error", "ORU-002", "ORU message has no OBX segment", "OBX"))
        for obx in obx_segments:
            if not obx.get(2):
                issues.append(_issue("warning", "OBX-001", "OBX-2 value type is missing", "OBX", 2))
            if not obx.get(3):
                issues.append(_issue("error", "OBX-002", "OBX-3 observation identifier is missing", "OBX", 3))
            if not obx.get(5) and not obx.is_null(5):
                issues.append(_issue("warning", "OBX-003", "OBX-5 observation value is empty and not an explicit null", "OBX", 5))

    if code in {"ORM", "OML"}:
        if message.segment("ORC") is None:
            issues.append(_issue("warning", "ORM-001", "order message has no ORC segment", "ORC"))
        if message.segment("OBR") is None:
            issues.append(_issue("error", "ORM-002", "order message has no OBR segment", "OBR"))

    if code == "SIU":
        sch = message.segment("SCH")
        if sch is None:
            issues.append(_issue("error", "SCH-001", "SIU message has no SCH segment", "SCH"))
        else:
            if not sch.get(1) and not sch.get(2):
                issues.append(_issue("warning", "SCH-002", "SCH-1 and SCH-2 are both empty; appointment has no identifier", "SCH", 1))
        if not any(message.segments_named(name) for name in ("AIS", "AIG", "AIL", "AIP")):
            issues.append(_issue("warning", "SIU-001", "SIU has no AIS/AIG/AIL/AIP resource segment", "RGS"))

    if code == "MDM":
        txa = message.segment("TXA")
        if txa is None:
            issues.append(_issue("error", "TXA-001", "MDM message has no TXA segment", "TXA"))
        else:
            if not txa.get(12):
                issues.append(_issue("warning", "TXA-002", "TXA-12 unique document number is empty", "TXA", 12))
            if not txa.get(17):
                issues.append(_issue("warning", "TXA-003", "TXA-17 document completion status is empty", "TXA", 17))
        if message.message_type[1] in {"T02", "T04", "T06", "T08", "T10"}:
            ed_obx = [obx for obx in message.segments_named("OBX") if obx.get(2).upper() == "ED"]
            if not ed_obx:
                issues.append(_issue("error", "MDM-001", "content-bearing MDM trigger has no ED OBX segment", "OBX"))

    if strict:
        issues = [
            Issue("error" if item.severity == "warning" else item.severity, item.code, item.message, item.segment, item.field)
            for item in issues
        ]
    return issues


# --------------------------------------------------------------------------
# ACK
# --------------------------------------------------------------------------

def build_ack(
    message: Message,
    code: str = "AA",
    text: str = "",
    *,
    control_id: str | None = None,
    error_code: str = "207",
    timestamp: datetime | None = None,
    echo_trigger: bool = False,
) -> Message:
    """Build an application acknowledgement for an inbound message.

    MSH-10 of the inbound message is echoed in MSA-2. A new control ID is
    generated for the ACK itself. AA/AE/AR are application acknowledgements;
    CA/CE/CR are commit acknowledgements and only valid when the original
    message requested one.
    """
    if code not in ACK_CODES:
        raise Hl7Error(f"invalid acknowledgement code {code!r}")
    msh = message.require_segment("MSH")
    seps = message.separators
    stamp = (timestamp or datetime.now()).strftime("%Y%m%d%H%M%S")

    if echo_trigger:
        trigger = message.message_type[1]
        ack_type = f"ACK^{trigger}^ACK" if trigger else "ACK"
    else:
        ack_type = "ACK"

    def raw(number: int) -> str:
        return msh.field(number)

    msh_fields = [
        seps.field,
        seps.encoding_characters,
        raw(5) or raw(3),
        raw(6) or raw(4),
        raw(3),
        raw(4),
        stamp,
        "",
        ack_type,
        control_id or uuid.uuid4().hex.upper()[:20],
        raw(11) or "P",
        raw(12) or "2.5.1",
    ]

    ack = Message([Segment("MSH", msh_fields, seps)], seps)
    original_id = raw(10)
    ack.append(Segment("MSA", [code, original_id], seps))

    if code in {"AE", "AR", "CE", "CR"}:
        message_text = escape_text(text or _ack_text(code), seps, line_breaks=True)
        err = Segment("ERR", ["", "", f"{error_code}^{message_text}^HL70357", "E"], seps)
        ack.append(err)
    return ack


def _ack_text(code: str) -> str:
    return {
        "AA": "Application accept",
        "AE": "Application error",
        "AR": "Application reject",
        "CA": "Commit accept",
        "CE": "Commit error",
        "CR": "Commit reject",
    }.get(code, "Acknowledgement")


def build_minimal_ack(raw: bytes | str, text: str = "Unable to parse message", *, code: str = "AE") -> Message:
    """Last-resort ACK when the inbound message cannot be parsed at all."""
    try:
        return build_ack(Message.parse(raw, strict=False), code=code, text=text, error_code="200")
    except Hl7Error:
        pass

    text_value = raw.decode("latin-1", "replace") if isinstance(raw, bytes) else raw
    control_id = ""
    first_line = text_value.replace("\r", "\n").split("\n")[0]
    if first_line.startswith("MSH"):
        separator = first_line[3] if len(first_line) > 3 else "|"
        parts = first_line.split(separator)
        if len(parts) > 9:
            control_id = parts[9]

    seps = Separators()
    now = datetime.now().strftime("%Y%m%d%H%M%S")
    msh = Segment(
        "MSH",
        [seps.field, seps.encoding_characters, "", "", "", "", now, "", "ACK", uuid.uuid4().hex.upper()[:20], "P", "2.5.1"],
        seps,
    )
    msa = Segment("MSA", [code, control_id], seps)
    err = Segment("ERR", ["", "", f"200^{escape_text(text, seps, line_breaks=True)}^HL70357", "E"], seps)
    return Message([msh, msa, err], seps)


# --------------------------------------------------------------------------
# MLLP
# --------------------------------------------------------------------------

def mllp_frame(payload: bytes) -> bytes:
    if not isinstance(payload, (bytes, bytearray)):
        raise TypeError("payload must be bytes")
    return VT + bytes(payload) + FS + CR


def mllp_unframe(buffer: bytes) -> tuple[bytes | None, bytes]:
    """Return (payload, remainder); payload is None until FS was received."""
    start = buffer.find(VT)
    if start == -1:
        start = 0
    end = buffer.find(FS, start + 1)
    if end == -1:
        return None, buffer
    if buffer[start:start + 1] == VT:
        payload = buffer[start + 1:end]
    else:
        payload = buffer[start:end]
    rest = buffer[end + 1:]
    if rest.startswith(CR):
        rest = rest[1:]
    elif rest.startswith(b"\n"):
        rest = rest[1:]
    return payload, rest


def send_mllp(
    message: Message | str | bytes,
    host: str,
    port: int,
    *,
    timeout: float = 10.0,
    encoding: str | None = None,
    tls: bool = False,
    ca_file: str | None = None,
    cert_file: str | None = None,
    key_file: str | None = None,
    insecure: bool = False,
    max_ack_bytes: int = 1_000_000,
) -> Message:
    """Send one message over MLLP and return the received acknowledgement."""
    if isinstance(message, Message):
        payload = message.to_bytes(encoding)
    elif isinstance(message, str):
        payload = message.encode(encoding or "utf-8")
    else:
        payload = bytes(message)

    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        if tls:
            context = ssl.create_default_context(cafile=ca_file)
            if cert_file:
                context.load_cert_chain(cert_file, key_file)
            if insecure:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            sock = context.wrap_socket(sock, server_hostname=host)

        sock.sendall(mllp_frame(payload))
        buffer = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buffer += chunk
            if len(buffer) > max_ack_bytes:
                raise Hl7Error("acknowledgement exceeds size limit")
            ack_payload, _rest = mllp_unframe(buffer)
            if ack_payload is not None:
                return Message.parse(ack_payload, encoding=encoding)
        if buffer.strip():
            return Message.parse(buffer, encoding=encoding)
        raise Hl7Error("connection closed before an acknowledgement was received")
    finally:
        try:
            sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------

def strip_mllp(data: bytes | str) -> bytes | str:
    """Remove MLLP framing if present."""
    if isinstance(data, bytes):
        return _strip_mllp_bytes(data)
    return _strip_mllp_text(data)


def _strip_mllp_bytes(data: bytes) -> bytes:
    if data.startswith(VT):
        data = data[1:]
    if data.endswith(FS + CR):
        data = data[:-2]
    elif data.endswith(FS + b"\n"):
        data = data[:-2]
    elif data.endswith(FS):
        data = data[:-1]
    return data


def _strip_mllp_text(text: str) -> str:
    if text.startswith("\x0b"):
        text = text[1:]
    if text.endswith("\x1c\r"):
        text = text[:-2]
    elif text.endswith("\x1c\n"):
        text = text[:-2]
    elif text.endswith("\x1c"):
        text = text[:-1]
    return text


def _decode(data: bytes | str, encoding: str | None) -> tuple[str, str]:
    if isinstance(data, str):
        return data, encoding or "utf-8"
    if encoding:
        return data.decode(encoding), encoding
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("latin-1"), "latin-1"


def _trim_trailing_empty(fields: list[str]) -> list[str]:
    trimmed = list(fields)
    while len(trimmed) > 1 and trimmed[-1] == "":
        trimmed.pop()
    return trimmed


def _drop_batch_headers(lines: list[str]) -> list[str]:
    index = 0
    while index < len(lines) and lines[index].startswith(("FHS", "BHS")):
        index += 1
    return lines[index:]
