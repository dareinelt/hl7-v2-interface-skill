#!/usr/bin/env python3
"""Bidirectional mapping between HL7 v2.x and FHIR R4 (standard library only).

Supported directions:
  * ADT^A01/A02/A03/A04/A05/A08  <->  Patient + Encounter (+ Practitioner)
  * ORU^R01                      <->  Patient + DiagnosticReport + Observation
  * ORM^O01 / OML^O21            <->  Patient + ServiceRequest

The mapping is deliberately conservative: it refuses to invent identifier
systems, units, codes or statuses. Every gap is reported as a warning and the
element is left out or mapped to the FHIR "unknown" value.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    from hl7core import Hl7Error, Message, Segment, Separators, escape_text, unescape_text
except ImportError:  # pragma: no cover - executed as a module
    from .hl7core import Hl7Error, Message, Segment, Separators, escape_text, unescape_text  # type: ignore

# --------------------------------------------------------------------------
# Code systems
# --------------------------------------------------------------------------

LOINC = "http://loinc.org"
SNOMED = "http://snomed.info/sct"
UCUM = "http://unitsofmeasure.org"
V2_0203 = "http://terminology.hl7.org/CodeSystem/v2-0203"
V2_0004 = "http://terminology.hl7.org/CodeSystem/v2-0004"
V2_0001 = "http://terminology.hl7.org/CodeSystem/v2-0001"
V2_0002 = "http://terminology.hl7.org/CodeSystem/v2-0002"
V2_0074 = "http://terminology.hl7.org/CodeSystem/v2-0074"
V2_0076 = "http://terminology.hl7.org/CodeSystem/v2-0076"
V2_0078 = "http://terminology.hl7.org/CodeSystem/v2-0078"
V2_0119 = "http://terminology.hl7.org/CodeSystem/v2-0119"
V2_0125 = "http://terminology.hl7.org/CodeSystem/v2-0125"
V2_0296 = "http://terminology.hl7.org/CodeSystem/v2-0296"
V2_0485 = "http://terminology.hl7.org/CodeSystem/v2-0485"
V3_ACT_CODE = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
V3_MARITAL = "http://terminology.hl7.org/CodeSystem/v3-MaritalStatus"
V3_PARTICIPATION = "http://terminology.hl7.org/CodeSystem/v3-ParticipationType"

ICD10 = "http://hl7.org/fhir/sid/icd-10"
ICD9 = "http://hl7.org/fhir/sid/icd-9-cm"

DEFAULT_NAMESPACE = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")  # UUID NAMESPACE_URL

GENDER_TO_FHIR = {"M": "male", "F": "female", "O": "other", "U": "unknown", "A": "other", "N": "unknown"}
GENDER_FROM_FHIR = {"male": "M", "female": "F", "other": "O", "unknown": "U"}

IDENTIFIER_TYPE_CODES = {
    "MR": "Medical record number",
    "SS": "Social Security number",
    "PI": "Patient internal identifier",
    "PN": "Person number",
    "DL": "Driver's license number",
    "PPN": "Passport number",
    "PRN": "Provider number",
    "FI": "Facility identifier",
    "PLAC": "Placer identifier",
    "FILL": "Filler identifier",
    "VN": "Visit number",
}
IDENTIFIER_TYPE_BY_SYSTEM_DISPLAY = {value: key for key, value in IDENTIFIER_TYPE_CODES.items()}

MARITAL_V2_TO_V3 = {"S": "S", "M": "M", "D": "D", "W": "W", "C": "L", "U": "U", "A": "A", "P": "P", "I": "I"}
MARITAL_V3_TO_V2 = {"S": "S", "M": "M", "D": "D", "W": "W", "L": "C", "U": "U", "A": "A", "P": "P", "I": "I"}

CLASS_V2_TO_FHIR = {
    "I": ("IMP", "inpatient encounter"),
    "O": ("AMB", "ambulatory"),
    "E": ("EMER", "emergency"),
    "P": ("PRENC", "pre-admission"),
    "R": ("AMB", "recurring patient"),
    "B": ("IMP", "obstetrics"),
    "C": ("AMB", "commercial account"),
    "N": ("AMB", "not applicable"),
    "U": ("AMB", "unknown"),
}
CLASS_FHIR_TO_V2 = {"IMP": "I", "AMB": "O", "EMER": "E", "PRENC": "P", "SS": "I", "VR": "O", "HH": "O", "OBSENC": "I"}

ENCOUNTER_STATUS = {
    "A01": "in-progress",
    "A02": "in-progress",
    "A03": "finished",
    "A04": "in-progress",
    "A05": "planned",
    "A08": "in-progress",
    "A11": "cancelled",
    "A13": "cancelled",
}

ORDER_STATUS_V2_TO_FHIR = {
    "NW": "active",
    "OK": "completed",
    "CM": "completed",
    "CA": "revoked",
    "CR": "revoked",
    "DC": "revoked",
    "HD": "on-hold",
    "SC": "active",
    "IP": "active",
}
ORDER_STATUS_FHIR_TO_V2 = {"active": "NW", "completed": "CM", "revoked": "CA", "on-hold": "HD", "draft": "NW", "entered-in-error": "CA"}

PRIORITY_V2_TO_FHIR = {"S": "stat", "A": "urgent", "R": "routine", "P": "urgent", "T": "stat"}
PRIORITY_FHIR_TO_V2 = {"stat": "S", "urgent": "A", "asap": "A", "routine": "R", "routine_": "R"}

RESULT_STATUS = {
    "F": "final",
    "P": "preliminary",
    "C": "corrected",
    "X": "cancelled",
    "W": "entered-in-error",
    "D": "entered-in-error",
    "R": "registered",
    "I": "registered",
}
RESULT_STATUS_FHIR_TO_V2 = {
    "final": "F",
    "preliminary": "P",
    "corrected": "C",
    "amended": "C",
    "appended": "C",
    "cancelled": "X",
    "entered-in-error": "W",
    "registered": "P",
    "partial": "P",
    "unknown": "P",
}

INTERPRETATION_DISPLAY = {
    "N": "Normal",
    "A": "Abnormal",
    "AA": "Critical abnormal",
    "H": "High",
    "L": "Low",
    "HH": "Critical high",
    "LL": "Critical low",
    ">": "Above high threshold",
    "<": "Below low threshold",
    "S": "Susceptible",
    "R": "Resistant",
    "I": "Intermediate",
    "MS": "Moderately susceptible",
    "VS": "Very susceptible",
}

PARTICIPATION_DISPLAY = {
    "ATND": "attending physician",
    "REF": "referring physician",
    "CON": "consulting physician",
    "ADM": "admitting physician",
}


class MappingContext:
    def __init__(self, *, default_patient_system: str | None = None, deterministic: bool = True, namespace: uuid.UUID | None = None):
        self.warnings: list[str] = []
        self.default_patient_system = default_patient_system
        self.deterministic = deterministic
        self.namespace = namespace or DEFAULT_NAMESPACE
        self.refs: dict[tuple[str, str], str] = {}

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def ref(self, message: Message, kind: str, key: str) -> dict[str, str]:
        cache_key = (kind, key)
        if cache_key not in self.refs:
            seed = f"{message.control_id or 'no-control-id'}|{kind}|{key}"
            value = f"urn:uuid:{uuid.uuid5(self.namespace, seed)}" if self.deterministic else f"urn:uuid:{uuid.uuid4()}"
            self.refs[cache_key] = value
        return {"reference": self.refs[cache_key]}

    def full_url(self, message: Message, kind: str, key: str) -> str:
        self.ref(message, kind, key)
        return self.refs[(kind, key)]


# --------------------------------------------------------------------------
# Scalar helpers
# --------------------------------------------------------------------------

_TS_RE = re.compile(r"^(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?(?:\.(\d{1,4}))?([+-]\d{4})?$")
_ISO_RE = re.compile(
    r"^(\d{4})(?:-(\d{2})(?:-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(Z|[+-]\d{2}:\d{2})?)?)?)?$"
)


def ts_to_iso(value: str, *, date_only: bool = False) -> str | None:
    """HL7 TS/DTM -> FHIR date/dateTime. Partial dates stay partial."""
    text = (value or "").strip()
    if not text or text == '""':
        return None
    match = _TS_RE.match(text)
    if not match:
        return None
    year, month, day, hour, minute, second, fraction, offset = match.groups()
    if date_only or hour is None:
        if day and month:
            return f"{year}-{month}-{day}"
        if month:
            return f"{year}-{month}"
        return year
    if not (month and day):
        return None
    stamp = f"{year}-{month}-{day}T{hour}:{minute or '00'}:{second or '00'}"
    if fraction:
        stamp += "." + fraction
    if offset:
        stamp += f"{offset[:3]}:{offset[3:]}"
    return stamp


def iso_to_ts(value: str | None, *, date_only: bool = False) -> str:
    """FHIR date/dateTime -> HL7 TS/DTM. Returns '' for unusable input."""
    if not value:
        return ""
    match = _ISO_RE.match(str(value).strip())
    if not match:
        return ""
    year, month, day, hour, minute, second, offset = match.groups()
    if date_only or hour is None:
        return f"{year}{month or ''}{day or ''}"
    stamp = f"{year}{month}{day}{hour}{minute}{second or '00'}"
    if offset:
        stamp += "+0000" if offset == "Z" else offset.replace(":", "")
    return stamp


def _plain(value: str, seps: Separators) -> str:
    return unescape_text(value, seps).strip()


def assigning_authority_system(components: Sequence[str]) -> str | None:
    """Turn an HD (namespace, universal ID, universal ID type) into a system URI.

    OIDs and UUIDs keep their canonical form. Everything else becomes a
    namespace URI that is clearly marked as local. Production deployments
    should pass `--patient-system` or align the OID with the receiver.
    """
    namespace = components[0].strip() if len(components) > 0 else ""
    universal_id = components[1].strip() if len(components) > 1 else ""
    id_type = components[2].strip().upper() if len(components) > 2 else ""

    if universal_id:
        if id_type == "UUID":
            return f"urn:uuid:{universal_id}"
        if re.fullmatch(r"\d+(?:\.\d+)+", universal_id):
            return f"urn:oid:{universal_id}"
        if universal_id.startswith("urn:"):
            return universal_id
        return f"urn:hl7:authority:{_slug(universal_id)}"
    if namespace:
        return f"urn:hl7:facility:{_slug(namespace)}"
    return None


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-").lower() or "unnamed"


def coding_system_uri(code: str | None) -> str | None:
    if not code:
        return None
    normalized = code.strip().upper()
    mapping = {
        "LN": LOINC,
        "LOINC": LOINC,
        "SCT": SNOMED,
        "SNM": SNOMED,
        "SNOMED": SNOMED,
        "I9": ICD9,
        "I10": ICD10,
        "ICD10": ICD10,
    }
    if normalized.startswith("HL7") and normalized[3:].isdigit():
        return f"http://terminology.hl7.org/CodeSystem/v2-{normalized[3:]}"
    return mapping.get(normalized)


def coding_system_code(uri: str | None) -> str | None:
    if not uri:
        return None
    mapping = {
        LOINC: "LN",
        SNOMED: "SCT",
        ICD10: "I10",
        ICD9: "I9",
        UCUM: "UCUM",
    }
    if uri.startswith("http://terminology.hl7.org/CodeSystem/v2-"):
        return "HL7" + uri.rsplit("v2-", 1)[-1]
    if uri.rstrip("/") == "http://terminology.hl7.org/CodeSystem/v3-Confidentiality":
        return "HL70272"
    return mapping.get(uri.rstrip("/"))


def codeable_concept(raw: str, seps: Separators) -> dict[str, Any] | None:
    """CE/CWE/CWE components -> FHIR CodeableConcept."""
    if not raw:
        return None
    components = [_plain(part, seps) for part in raw.split(seps.component)]
    code = components[0] if components else ""
    text = components[1] if len(components) > 1 else ""
    system_code = components[2] if len(components) > 2 else ""
    if not code and not text and len(components) >= 5:
        code = components[3]
        text = components[4]
        system_code = components[5] if len(components) > 5 else ""

    if code == '""':
        return None
    if not code and not text:
        return None

    concept: dict[str, Any] = {}
    if code:
        coding: dict[str, Any] = {"code": code}
        system = coding_system_uri(system_code)
        if system:
            coding["system"] = system
        if text:
            coding["display"] = text
        concept["coding"] = [coding]
    if text:
        concept["text"] = text
    elif code:
        concept["text"] = code
    return concept


def concept_to_ce(concept: dict[str, Any] | None, seps: Separators) -> str:
    """FHIR CodeableConcept -> escaped CE/CWE field value."""
    if not concept:
        return ""
    coding = (concept.get("coding") or [{}])[0]
    code = str(coding.get("code") or "")
    text = str(coding.get("display") or concept.get("text") or "")
    system = coding_system_code(coding.get("system")) or ""
    parts = [escape_text(code, seps), escape_text(text, seps)]
    if system:
        parts.append(escape_text(system, seps))
    return _join_components(parts, seps)


def _join_components(parts: Iterable[str], seps: Separators) -> str:
    values = list(parts)
    while values and values[-1] == "":
        values.pop()
    return seps.component.join(values)


def _number_text(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return str(value)
    return f"{number:.15g}"


def _concept_key(concept: dict[str, Any] | None) -> str | None:
    if not concept:
        return None
    coding = (concept.get("coding") or [{}])[0]
    return coding.get("code") or concept.get("text")


def _quantity(raw_value: str, raw_units: str, seps: Separators, ctx: MappingContext, field: str) -> dict[str, Any] | None:
    text = _plain(raw_value, seps)
    try:
        number = float(text)
    except ValueError:
        ctx.warn(f"{field}: numeric value {text!r} is not a number; mapped as string")
        return None
    if not math.isfinite(number):
        ctx.warn(f"{field}: non-finite numeric value; dropped")
        return None
    quantity: dict[str, Any] = {"value": number}
    unit_components = [_plain(part, seps) for part in raw_units.split(seps.component)] if raw_units else []
    unit_code = unit_components[0] if unit_components else ""
    unit_text = unit_components[1] if len(unit_components) > 1 else ""
    unit_system = unit_components[2] if len(unit_components) > 2 else ""
    if unit_code:
        quantity["code"] = unit_code
        quantity["unit"] = unit_text or unit_code
        if unit_system.upper() in {"UCUM", "UCUM/ISO"} or not unit_system:
            quantity["system"] = UCUM
    elif unit_text:
        quantity["unit"] = unit_text
        ctx.warn(f"{field}: unit has no code; Quantity.unit contains text only")
    else:
        ctx.warn(f"{field}: no unit in OBX-6; Quantity has no unit")
    return quantity


def _patient_identifier(raw: str, seps: Separators, ctx: MappingContext) -> dict[str, Any] | None:
    raw_components = raw.split(seps.component)
    components = [_plain(part, seps) for part in raw_components]
    if not components:
        return None
    value = components[0].strip() if components[0] else ""
    if not value:
        return None
    identifier: dict[str, Any] = {"value": value}
    authority = raw_components[3].split(seps.subcomponent) if len(raw_components) > 3 and raw_components[3] else []
    system = assigning_authority_system(authority) if authority else None
    if system:
        identifier["system"] = system
    elif ctx.default_patient_system:
        identifier["system"] = ctx.default_patient_system
    type_code = components[4].strip() if len(components) > 4 else ""
    if type_code:
        identifier["type"] = {
            "coding": [{"system": V2_0203, "code": type_code, "display": IDENTIFIER_TYPE_CODES.get(type_code, type_code)}],
            "text": IDENTIFIER_TYPE_CODES.get(type_code, type_code),
        }
    return identifier


def _identifier_from_ei(raw: str, seps: Separators) -> dict[str, Any] | None:
    """EI (entity identifier) -> FHIR Identifier."""
    components = [_plain(part, seps) for part in raw.split(seps.component)]
    value = components[0].strip() if components else ""
    if not value:
        return None
    identifier: dict[str, Any] = {"value": value}
    namespace = components[1].strip() if len(components) > 1 else ""
    universal = components[2].strip() if len(components) > 2 else ""
    if universal:
        identifier["system"] = f"urn:oid:{universal}" if re.fullmatch(r"\d+(?:\.\d+)+", universal) else f"urn:hl7:authority:{_slug(universal)}"
    elif namespace:
        identifier["system"] = f"urn:hl7:namespace:{_slug(namespace)}"
    type_code = components[3].strip() if len(components) > 3 else ""
    if type_code:
        identifier["type"] = {"coding": [{"system": V2_0203, "code": type_code}]}
    return identifier


def _practitioner_from_xcn(raw: str, seps: Separators, ctx: MappingContext, field: str) -> tuple[str, dict[str, Any]] | None:
    raw_components = raw.split(seps.component)
    components = [_plain(part, seps) for part in raw_components]
    if not components or not any(components):
        return None
    identifier_value = components[0] if components else ""
    family = components[1] if len(components) > 1 else ""
    given = components[2] if len(components) > 2 else ""
    middle = components[3] if len(components) > 3 else ""
    suffix = components[4] if len(components) > 4 else ""
    prefix = components[5] if len(components) > 5 else ""
    if not (identifier_value or family or given):
        ctx.warn(f"{field}: practitioner has neither identifier nor name; skipped")
        return None

    key = identifier_value or f"{family}|{given}"
    practitioner: dict[str, Any] = {"resourceType": "Practitioner"}
    if identifier_value:
        identifier: dict[str, Any] = {"value": identifier_value}
        authority = raw_components[8].split(seps.subcomponent) if len(raw_components) > 8 and raw_components[8] else []
        system = assigning_authority_system(authority) if authority else None
        if system:
            identifier["system"] = system
        practitioner["identifier"] = [identifier]
    if family or given:
        name: dict[str, Any] = {}
        if family:
            name["family"] = family
        given_list = [item for item in (given, middle) if item]
        if given_list:
            name["given"] = given_list
        if prefix:
            name["prefix"] = [prefix]
        if suffix:
            name["suffix"] = [suffix]
        practitioner["name"] = [name]
    return key, practitioner


# --------------------------------------------------------------------------
# HL7 -> FHIR
# --------------------------------------------------------------------------

def patient_from_message(message: Message, ctx: MappingContext) -> dict[str, Any] | None:
    pid = message.segment("PID")
    if pid is None:
        ctx.warn("no PID segment; no Patient resource produced")
        return None
    seps = message.separators

    patient: dict[str, Any] = {"resourceType": "Patient"}

    identifiers = [item for item in (_patient_identifier(rep, seps, ctx) for rep in pid.repetitions(3)) if item]
    if identifiers:
        patient["identifier"] = identifiers
    else:
        ctx.warn("PID-3 has no usable identifier")

    names: list[dict[str, Any]] = []
    for index, rep in enumerate(pid.repetitions(5)):
        components = [_plain(part, seps) for part in rep.split(seps.component)]
        family = components[0] if components else ""
        given = components[1] if len(components) > 1 else ""
        middle = components[2] if len(components) > 2 else ""
        suffix = components[3] if len(components) > 3 else ""
        prefix = components[4] if len(components) > 4 else ""
        if not (family or given or middle):
            continue
        name: dict[str, Any] = {"use": "official" if index == 0 else "usual"}
        if family:
            name["family"] = family
        given_list = [item for item in (given, middle) if item]
        if given_list:
            name["given"] = given_list
        if prefix:
            name["prefix"] = [prefix]
        if suffix:
            name["suffix"] = [suffix]
        names.append(name)
    if names:
        patient["name"] = names
    else:
        ctx.warn("PID-5 has no usable name")

    birth_date = ts_to_iso(pid.get(7), date_only=True)
    if birth_date:
        patient["birthDate"] = birth_date
    gender_code = _plain(pid.get(8), seps).upper()
    if gender_code:
        patient["gender"] = GENDER_TO_FHIR.get(gender_code, "unknown")
        if gender_code not in GENDER_TO_FHIR:
            ctx.warn(f"PID-8 gender code {gender_code!r} is not in the standard table; mapped to unknown")

    addresses: list[dict[str, Any]] = []
    for rep in pid.repetitions(11):
        components = [_plain(part, seps) for part in rep.split(seps.component)]
        address: dict[str, Any] = {}
        line = [item for item in (components[0] if components else "", components[1] if len(components) > 1 else "") if item]
        if line:
            address["line"] = line
        if len(components) > 2 and components[2]:
            address["city"] = components[2]
        if len(components) > 3 and components[3]:
            address["state"] = components[3]
        if len(components) > 4 and components[4]:
            address["postalCode"] = components[4]
        if len(components) > 5 and components[5]:
            address["country"] = components[5]
        if address:
            addresses.append(address)
    if addresses:
        patient["address"] = addresses

    telecom: list[dict[str, Any]] = []
    for number, use in ((13, "home"), (14, "work")):
        for rep in pid.repetitions(number):
            components = [_plain(part, seps) for part in rep.split(seps.component)]
            equipment = components[2].upper() if len(components) > 2 else ""
            email = components[3] if len(components) > 3 else ""
            value = email or (components[0] if components else "")
            if not value:
                continue
            entry: dict[str, Any] = {"value": value, "use": use}
            if email or equipment in {"INTERNET", "X.400"}:
                entry["system"] = "email" if email else "other"
            elif equipment in {"PH", "CP"} or not equipment:
                entry["system"] = "phone"
            elif equipment == "FX":
                entry["system"] = "fax"
            elif equipment == "BP":
                entry["system"] = "pager"
            else:
                entry["system"] = "other"
            telecom.append(entry)
    if telecom:
        patient["telecom"] = telecom

    marital = codeable_concept(pid.get(16), seps)
    if marital:
        coding = (marital.get("coding") or [{}])[0]
        code = coding.get("code")
        v3_code = MARITAL_V2_TO_V3.get(code or "", "")
        if v3_code:
            patient["maritalStatus"] = {
                "coding": [{"system": V3_MARITAL, "code": v3_code, "display": coding.get("display") or v3_code}],
                "text": marital.get("text"),
            }
        else:
            patient["maritalStatus"] = marital
            if code:
                ctx.warn(f"PID-16 marital code {code!r} has no v3-MaritalStatus equivalent")

    language = codeable_concept(pid.get(15), seps)
    if language:
        patient["communication"] = [{"language": language, "preferred": True}]

    deceased_at = ts_to_iso(pid.get(29))
    if deceased_at:
        patient["deceasedDateTime"] = deceased_at
    elif _plain(pid.get(30), seps).upper() == "Y":
        patient["deceasedBoolean"] = True

    return patient


def _encounter_participants(message: Message, ctx: MappingContext) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pv1 = message.segment("PV1")
    if pv1 is None:
        return [], []
    seps = message.separators
    participants: list[dict[str, Any]] = []
    practitioners: list[dict[str, Any]] = []
    seen: set[str] = set()
    for field_number, role in ((7, "ATND"), (8, "REF"), (9, "CON"), (17, "ADM")):
        raw = pv1.get(field_number)
        if not raw:
            continue
        parsed = _practitioner_from_xcn(raw, seps, ctx, f"PV1-{field_number}")
        if parsed is None:
            continue
        key, practitioner = parsed
        reference = ctx.ref(message, "Practitioner", key)
        if key not in seen:
            seen.add(key)
            practitioners.append(practitioner)
        participants.append({
            "type": [{"coding": [{"system": V3_PARTICIPATION, "code": role, "display": PARTICIPATION_DISPLAY.get(role, role)}]}],
            "individual": dict(reference),
        })
    return participants, practitioners


def encounter_from_message(message: Message, ctx: MappingContext, patient_ref: dict[str, str] | None) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    pv1 = message.segment("PV1")
    if pv1 is None:
        return None, []
    seps = message.separators
    trigger = message.message_type[1]

    encounter: dict[str, Any] = {
        "resourceType": "Encounter",
        "status": ENCOUNTER_STATUS.get(trigger, "unknown"),
    }
    if patient_ref:
        encounter["subject"] = patient_ref

    class_code = _plain(pv1.get(2), seps).upper()
    fhir_class = CLASS_V2_TO_FHIR.get(class_code)
    if fhir_class:
        code, display = fhir_class
        encounter["class"] = {"system": V3_ACT_CODE, "code": code, "display": display}
    else:
        encounter["class"] = {"system": V3_ACT_CODE, "code": "AMB", "display": "ambulatory"}
        ctx.warn(f"PV1-2 patient class {class_code or '(empty)'!r} is not mapped; used AMB")

    identifiers: list[dict[str, Any]] = []
    visit = _patient_identifier(pv1.get(19), seps, ctx)
    if visit:
        identifiers.append(visit)
    if identifiers:
        encounter["identifier"] = identifiers

    period: dict[str, Any] = {}
    start = ts_to_iso(pv1.get(44)) or ts_to_iso(message.get("EVN-2"))
    end = ts_to_iso(pv1.get(45))
    if start:
        period["start"] = start
    if end:
        period["end"] = end
    if period:
        encounter["period"] = period

    location_components = [_plain(part, seps) for part in pv1.get(3).split(seps.component)]
    display_parts = [
        location_components[index]
        for index in (3, 6, 7, 0, 1, 2)
        if len(location_components) > index and location_components[index]
    ]
    if display_parts:
        encounter["location"] = [{"location": {"display": " / ".join(display_parts)}}]

    participants, practitioners = _encounter_participants(message, ctx)
    if participants:
        encounter["participant"] = participants

    service = _plain(pv1.get(10), seps)
    if service:
        encounter["serviceType"] = {"text": service}

    return encounter, practitioners


def _service_request_from_order(
    message: Message,
    ctx: MappingContext,
    patient_ref: dict[str, str] | None,
    *,
    require_orc: bool = False,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    orc = message.segment("ORC")
    obr = message.segment("OBR")
    if obr is None or (require_orc and orc is None):
        return None, []
    seps = message.separators
    request: dict[str, Any] = {"resourceType": "ServiceRequest", "intent": "order"}

    status_code = _plain(orc.get(1), seps).upper() if orc else ""
    request["status"] = ORDER_STATUS_V2_TO_FHIR.get(status_code, "active")
    if orc and not status_code:
        ctx.warn("ORC-1 order control code is empty; used active")

    identifiers: list[dict[str, Any]] = []
    if orc:
        for raw in (orc.get(2), orc.get(3)):
            identifier = _identifier_from_ei(raw, seps)
            if identifier:
                identifiers.append(identifier)
    if obr.get(2) or obr.get(3):
        for raw in (obr.get(2), obr.get(3)):
            identifier = _identifier_from_ei(raw, seps)
            if identifier and identifier not in identifiers:
                identifiers.append(identifier)
    if identifiers:
        request["identifier"] = identifiers

    code = codeable_concept(obr.get(4), seps)
    if code:
        request["code"] = code
    else:
        request["code"] = {"text": "Unspecified order"}
        ctx.warn("OBR-4 service identifier is empty; used a text-only code")

    if patient_ref:
        request["subject"] = patient_ref

    occurrence = ts_to_iso(obr.get(7)) or ts_to_iso(obr.get(6))
    if occurrence:
        request["occurrenceDateTime"] = occurrence
    if orc:
        authored = ts_to_iso(orc.get(9))
        if authored:
            request["authoredOn"] = authored

    priority = _plain(obr.get(5) or (orc.get(7) if orc else ""), seps).upper()
    if priority:
        request["priority"] = PRIORITY_V2_TO_FHIR.get(priority, "routine")
        if priority not in PRIORITY_V2_TO_FHIR:
            ctx.warn(f"priority {priority!r} is not mapped; used routine")

    practitioners: list[dict[str, Any]] = []
    requester_field = (orc.get(12) if orc else "") or obr.get(16)
    if requester_field:
        parsed = _practitioner_from_xcn(requester_field, seps, ctx, "ORC-12/OBR-16")
        if parsed:
            key, practitioner = parsed
            request["requester"] = ctx.ref(message, "Practitioner", key)
            practitioners.append(practitioner)

    if orc and orc.get(16):
        reason = codeable_concept(orc.get(16), seps)
        if reason:
            request["reasonCode"] = [reason]

    index = message.index_of("OBR")
    notes = message.notes_after(index)
    if notes:
        request["note"] = [{"text": text} for text in notes]

    return request, practitioners


def _observation_from_obx(obx: Segment, message: Message, ctx: MappingContext, patient_ref: dict[str, str] | None, notes: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    seps = message.separators
    observation: dict[str, Any] = {"resourceType": "Observation"}
    observation["status"] = RESULT_STATUS.get(_plain(obx.get(11), seps).upper(), "unknown")
    if observation["status"] == "unknown":
        ctx.warn(f"OBX-11 result status {obx.get(11)!r} is empty or not mapped; used unknown")

    observation["category"] = [{
        "coding": [{"system": V2_0074, "code": "LAB", "display": "Laboratory"}],
        "text": "Laboratory",
    }]

    code = codeable_concept(obx.get(3), seps)
    if code:
        observation["code"] = code
    else:
        ctx.warn("OBX-3 observation identifier is empty; Observation has no code")

    if patient_ref:
        observation["subject"] = patient_ref

    effective = ts_to_iso(obx.get(14))
    if effective:
        observation["effectiveDateTime"] = effective
    issued = ts_to_iso(obx.get(19))
    if issued:
        observation["issued"] = issued

    value_type = _plain(obx.get(2), seps).upper()
    raw_value = obx.get(5)
    if not value_type:
        value_type = "ST"
        if raw_value:
            ctx.warn("OBX-2 value type is empty; treated as ST")

    if obx.is_null(5):
        observation["dataAbsentReason"] = {"text": "unknown"}
    elif not raw_value:
        observation["dataAbsentReason"] = {"text": "not-performed"}
    elif value_type in {"NM", "SN"}:
        quantity = _quantity(raw_value, obx.get(6), seps, ctx, "OBX-5")
        if quantity:
            observation["valueQuantity"] = quantity
        else:
            observation["valueString"] = _plain(raw_value, seps)
    elif value_type in {"CE", "CWE", "CNE"}:
        concept = codeable_concept(raw_value, seps)
        if concept:
            observation["valueCodeableConcept"] = concept
        else:
            observation["valueString"] = _plain(raw_value, seps)
    elif value_type in {"DT", "DTM", "TS"}:
        temporal = ts_to_iso(raw_value, date_only=value_type == "DT")
        if temporal:
            observation["valueDateTime"] = temporal
        else:
            observation["valueString"] = _plain(raw_value, seps)
            ctx.warn(f"OBX-5 value {value_type} could not be parsed as a date; kept as string")
    elif value_type == "TM":
        digits = _plain(raw_value, seps)
        if len(digits) >= 4 and digits[:2].isdigit() and digits[2:4].isdigit():
            observation["valueTime"] = f"{digits[:2]}:{digits[2:4]}" + (f":{digits[4:6]}" if len(digits) >= 6 else "")
        else:
            observation["valueString"] = digits
    elif value_type == "NR":
        text = _plain(raw_value, seps)
        bounds = text.split(seps.subcomponent) if seps.subcomponent in text else text.split("-")
        try:
            low, high = float(bounds[0]), float(bounds[1])
        except (ValueError, IndexError):
            observation["valueString"] = text
        else:
            observation["valueRange"] = {"low": {"value": low}, "high": {"value": high}}
    elif value_type in {"ED", "RP"}:
        observation["valueString"] = _plain(raw_value, seps)
        ctx.warn(f"OBX-2 {value_type} (embedded/binary data) is not converted; kept as text")
    else:
        observation["valueString"] = _plain(raw_value, seps)

    interpretations = [
        {
            "coding": [{
                "system": V2_0078,
                "code": code,
                "display": INTERPRETATION_DISPLAY.get(code, code),
            }]
        }
        for code in (_plain(rep, seps) for rep in obx.repetitions(8))
        if code
    ]
    if interpretations:
        observation["interpretation"] = interpretations

    if obx.get(7):
        observation["referenceRange"] = [{"text": _plain(obx.get(7), seps)}]

    method = codeable_concept(obx.get(17), seps)
    if method:
        observation["method"] = method
    body_site = codeable_concept(obx.get(20), seps)
    if body_site:
        observation["bodySite"] = body_site

    practitioners: list[dict[str, Any]] = []
    performer_raw = obx.get(16)
    if performer_raw:
        parsed = _practitioner_from_xcn(performer_raw, seps, ctx, "OBX-16")
        if parsed:
            key, practitioner = parsed
            observation["performer"] = [ctx.ref(message, "Practitioner", key)]
            practitioners.append(practitioner)

    instance = _identifier_from_ei(obx.get(21), seps)
    if instance:
        observation["identifier"] = [instance]

    if notes:
        observation["note"] = [{"text": text} for text in notes]

    return observation, practitioners


def _oru_resources(
    message: Message,
    ctx: MappingContext,
    patient_ref: dict[str, str] | None,
    *,
    based_on: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    seps = message.separators
    entries: list[dict[str, Any]] = []
    practitioners: list[dict[str, Any]] = []
    seen_practitioners: set[str] = set()

    def add_practitioner(parsed: tuple[str, dict[str, Any]] | None) -> dict[str, str] | None:
        if parsed is None:
            return None
        key, practitioner = parsed
        if key not in seen_practitioners:
            seen_practitioners.add(key)
            practitioners.append(practitioner)
        return ctx.ref(message, "Practitioner", key)

    blocks: list[dict[str, Any]] = []
    for index, segment in enumerate(message.segments):
        if segment.name == "OBR":
            blocks.append({"obr": segment, "obx": [], "notes": message.notes_after(index)})
        elif segment.name == "OBX" and blocks:
            blocks[-1]["obx"].append((segment, message.notes_after(index)))

    for block_index, block in enumerate(blocks, start=1):
        obr: Segment = block["obr"]

        report: dict[str, Any] = {"resourceType": "DiagnosticReport"}
        status_code = _plain(obr.get(25), seps).upper()
        report["status"] = RESULT_STATUS.get(status_code, "final")
        if status_code and status_code not in RESULT_STATUS:
            ctx.warn(f"OBR-25 result status {obr.get(25)!r} is not mapped; used final")
        report["category"] = [{
            "coding": [{"system": V2_0074, "code": "LAB", "display": "Laboratory"}],
            "text": "Laboratory",
        }]

        code = codeable_concept(obr.get(4), seps)
        if code:
            report["code"] = code
        else:
            report["code"] = {"text": "Unspecified report"}
            ctx.warn("OBR-4 report code is empty; used text")

        if patient_ref:
            report["subject"] = patient_ref
        if based_on:
            report["basedOn"] = [based_on]

        identifiers: list[dict[str, Any]] = []
        for raw in (obr.get(3), obr.get(2)):
            identifier = _identifier_from_ei(raw, seps)
            if identifier:
                identifiers.append(identifier)
        if identifiers:
            report["identifier"] = identifiers
        report_key = (identifiers[0].get("value") if identifiers else "") or f"report-{block_index}"

        effective = ts_to_iso(obr.get(7)) or ts_to_iso(obr.get(22))
        if effective:
            report["effectiveDateTime"] = effective
        issued = ts_to_iso(obr.get(22))
        if issued:
            report["issued"] = issued

        performer_raw = obr.get(16)
        if performer_raw:
            reference = add_practitioner(_practitioner_from_xcn(performer_raw, seps, ctx, "OBR-16"))
            if reference:
                report["performer"] = [reference]

        if block["notes"]:
            report["note"] = [{"text": text} for text in block["notes"]]

        observation_entries: list[dict[str, Any]] = []
        result_refs: list[dict[str, str]] = []
        for sequence, (obx, notes) in enumerate(block["obx"], start=1):
            observation, extra = _observation_from_obx(obx, message, ctx, patient_ref, notes)
            for practitioner in extra:
                key = (practitioner.get("identifier") or [{}])[0].get("value") or "practitioner"
                if key not in seen_practitioners:
                    seen_practitioners.add(key)
                    practitioners.append(practitioner)
            observation_url = ctx.full_url(message, "Observation", f"{report_key}|{sequence}")
            observation_entries.append({"fullUrl": observation_url, "resource": observation})
            result_refs.append({"reference": observation_url})

        if result_refs:
            report["result"] = result_refs

        report_url = ctx.full_url(message, "DiagnosticReport", report_key)
        entries.append({"fullUrl": report_url, "resource": report})
        entries.extend(observation_entries)

    return entries, practitioners


def _bundle_request(resource: dict[str, Any]) -> dict[str, Any]:
    identifiers = resource.get("identifier") or []
    if resource.get("resourceType") == "Patient" and identifiers:
        first = identifiers[0]
        value = first.get("value", "")
        system = first.get("system")
        query = f"{system}|{value}" if system else value
        return {"method": "PUT", "url": f"Patient?identifier={query}"}
    return {"method": "POST", "url": resource.get("resourceType", "Resource")}


def hl7_to_fhir_report(
    message: Message,
    *,
    default_patient_system: str | None = None,
    bundle_type: str = "collection",
    deterministic: bool = True,
    include_provenance: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    ctx = MappingContext(default_patient_system=default_patient_system, deterministic=deterministic)
    entries: list[dict[str, Any]] = []

    code = message.message_type[0]
    patient = patient_from_message(message, ctx)
    patient_ref: dict[str, str] | None = None
    if patient:
        identifier_value = (patient.get("identifier") or [{}])[0].get("value") or "patient"
        full = ctx.full_url(message, "Patient", identifier_value)
        patient_ref = {"reference": full}
        entries.append({"fullUrl": full, "resource": patient})

    extras: list[dict[str, Any]] = []

    if code == "ADT":
        encounter, practitioners = encounter_from_message(message, ctx, patient_ref)
        if encounter:
            full = ctx.full_url(message, "Encounter", str(message.get("PV1-19") or message.control_id))
            entries.append({"fullUrl": full, "resource": encounter})
        for practitioner in practitioners:
            key = (practitioner.get("identifier") or [{}])[0].get("value") or str(len(extras))
            extras.append({"fullUrl": ctx.full_url(message, "Practitioner", key), "resource": practitioner})

    elif code == "ORU":
        if message.segment("OBR") is None:
            ctx.warn("ORU without OBR; only the Patient resource is produced")
        else:
            order, order_practitioners = _service_request_from_order(message, ctx, patient_ref, require_orc=True)
            order_ref: dict[str, str] | None = None
            if order:
                full = ctx.full_url(message, "ServiceRequest", str(message.get("ORC-2") or message.get("ORC-3") or "order"))
                order_ref = {"reference": full}
                entries.append({"fullUrl": full, "resource": order})
            resources, practitioners = _oru_resources(message, ctx, patient_ref, based_on=order_ref)
            for practitioner in order_practitioners + practitioners:
                key = (practitioner.get("identifier") or [{}])[0].get("value") or str(len(extras))
                extras.append({"fullUrl": ctx.full_url(message, "Practitioner", key), "resource": practitioner})
            entries.extend(resources)

    elif code in {"ORM", "OML"}:
        order, practitioners = _service_request_from_order(message, ctx, patient_ref)
        if order:
            full = ctx.full_url(message, "ServiceRequest", str(message.get("ORC-2") or message.get("ORC-3") or "order"))
            entries.append({"fullUrl": full, "resource": order})
        for practitioner in practitioners:
            key = (practitioner.get("identifier") or [{}])[0].get("value") or str(len(extras))
            extras.append({"fullUrl": ctx.full_url(message, "Practitioner", key), "resource": practitioner})

    elif code == "SIU":
        from hl7_siu_mdm import appointment_from_siu

        appointment, siu_extras = appointment_from_siu(message, ctx, patient_ref)
        if appointment:
            full = ctx.full_url(message, "Appointment", str(message.get("SCH-1") or message.control_id))
            entries.append({"fullUrl": full, "resource": appointment})
        extras.extend(siu_extras)

    elif code == "MDM":
        from hl7_siu_mdm import document_from_mdm

        document, mdm_extras, encounter = document_from_mdm(message, ctx, patient_ref)
        if encounter:
            full = ctx.full_url(message, "Encounter", str(message.get("PV1-19") or message.control_id))
            entries.append({"fullUrl": full, "resource": encounter})
        if document:
            full = ctx.full_url(message, "DocumentReference", str(message.get("TXA-12") or message.control_id))
            entries.append({"fullUrl": full, "resource": document})
        extras.extend(mdm_extras)

    else:
        ctx.warn(f"message type {code or '(empty)'} is not mapped; only the Patient resource is produced")

    # Practitioners before the resources that reference them for readability.
    entries = entries[:1] + extras + entries[1:]
    seen_urls: set[str] = set()
    unique_entries: list[dict[str, Any]] = []
    for entry in entries:
        url = entry.get("fullUrl", "")
        if url in seen_urls:
            continue
        seen_urls.add(url)
        unique_entries.append(entry)
    entries = unique_entries

    bundle: dict[str, Any] = {"resourceType": "Bundle", "type": bundle_type, "entry": entries}
    if bundle_type == "transaction":
        for entry in bundle["entry"]:
            entry["request"] = _bundle_request(entry["resource"])

    if include_provenance and entries:
        agents: list[dict[str, Any]] = []
        for field_number, label in ((3, "sender"), (5, "receiver")):
            name = message.get_text(f"MSH-{field_number}")
            if name:
                agents.append({
                    "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/provenance-participant-type", "code": "assembler"}]},
                    "who": {"display": f"{label}: {name}"},
                })
        provenance = {
            "resourceType": "Provenance",
            "target": [{"reference": entry["fullUrl"]} for entry in entries],
            "recorded": datetime.now(timezone.utc).isoformat(),
            "agent": agents or [{"who": {"display": "HL7 v2 interface"}}],
        }
        bundle["entry"].append({
            "fullUrl": f"urn:uuid:{uuid.uuid4()}",
            "resource": provenance,
        })

    return bundle, ctx.warnings


def hl7_to_fhir(message: Message, **kwargs: Any) -> dict[str, Any]:
    bundle, _warnings = hl7_to_fhir_report(message, **kwargs)
    return bundle


# --------------------------------------------------------------------------
# FHIR -> HL7
# --------------------------------------------------------------------------

def _index(resources: dict[str, Any] | Sequence[dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    by_url: dict[str, dict[str, Any]] = {}
    if isinstance(resources, dict):
        if resources.get("resourceType") == "Bundle":
            items = []
            for entry in resources.get("entry", []):
                resource = entry.get("resource")
                if not resource:
                    continue
                items.append(resource)
                full_url = entry.get("fullUrl")
                if full_url:
                    by_url[str(full_url).rstrip("/")] = resource
        else:
            items = [resources]
    else:
        items = list(resources)

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        if not isinstance(item, dict) or not item.get("resourceType"):
            continue
        grouped[item["resourceType"]].append(item)
        resource_id = item.get("id")
        if resource_id:
            by_url[f"{item['resourceType']}/{resource_id}"] = item
    return grouped, by_url


def _escape(value: Any, seps: Separators) -> str:
    return escape_text("" if value is None else str(value), seps)


def _escape_free(value: Any, seps: Separators) -> str:
    """Escape narrative text, encoding line breaks as \\.br\\."""
    return escape_text("" if value is None else str(value), seps, line_breaks=True)


def _name_components(name: dict[str, Any]) -> list[str]:
    given = name.get("given") or []
    return [
        name.get("family") or "",
        given[0] if len(given) > 0 else "",
        given[1] if len(given) > 1 else "",
        (name.get("suffix") or [""])[0],
        (name.get("prefix") or [""])[0],
    ]


def _xcn_from_practitioner(practitioner: dict[str, Any] | None, seps: Separators) -> str:
    if not practitioner:
        return ""
    identifier = (practitioner.get("identifier") or [{}])[0]
    name = (practitioner.get("name") or [{}])[0]
    parts = [
        _escape(identifier.get("value", ""), seps),
        _escape(name.get("family", ""), seps),
        _escape((name.get("given") or [""])[0], seps),
        _escape((name.get("given") or ["", ""])[1] if len(name.get("given") or []) > 1 else "", seps),
        _escape((name.get("suffix") or [""])[0], seps),
        _escape((name.get("prefix") or [""])[0], seps),
    ]
    return _join_components(parts, seps)


def _identifier_field(identifier: dict[str, Any], seps: Separators, *, type_default: str = "") -> str:
    value = _escape(identifier.get("value", ""), seps)
    system = str(identifier.get("system") or "")
    identifier_type = type_default
    coding = ((identifier.get("type") or {}).get("coding") or [{}])[0]
    if coding.get("code"):
        identifier_type = coding["code"]
    elif identifier.get("type", {}).get("text"):
        identifier_type = IDENTIFIER_TYPE_BY_SYSTEM_DISPLAY.get(identifier["type"]["text"], "")

    authority = ""
    if system.startswith("urn:oid:"):
        authority = seps.subcomponent.join(["", system[len("urn:oid:"):], "ISO"])
    elif system.startswith("urn:uuid:"):
        authority = seps.subcomponent.join(["", system[len("urn:uuid:"):], "UUID"])
    elif system:
        authority = _escape(_slug(system), seps)

    parts = [value, "", "", authority, _escape(identifier_type, seps)]
    return _join_components(parts, seps)


def _ei_field(identifier: dict[str, Any], seps: Separators) -> str:
    value = _escape(identifier.get("value", ""), seps)
    system = str(identifier.get("system") or "")
    namespace = ""
    universal = ""
    id_type = ""
    if system.startswith("urn:oid:"):
        universal = system[len("urn:oid:"):]
        id_type = "ISO"
    elif system.startswith("urn:hl7:namespace:"):
        namespace = system[len("urn:hl7:namespace:"):]
    elif system:
        namespace = _slug(system)
    return _join_components([value, _escape(namespace, seps), _escape(universal, seps), _escape(id_type, seps)], seps)


def _lookup_practitioner(reference: str, practitioners: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if not reference:
        return None
    ref = reference.strip()
    found = practitioners.get(ref) or practitioners.get(ref.rstrip("/"))
    if found:
        return found
    tail = ref.rstrip("/").rsplit("/", 1)[-1]
    return practitioners.get(tail)


def _build_patient_segments(patient: dict[str, Any], seps: Separators, ctx: MappingContext) -> list[Segment]:
    names = patient.get("name") or [{}]
    official = next((item for item in names if item.get("use") == "official"), names[0])
    family, given, middle, suffix, prefix = _name_components(official)

    addresses = patient.get("address") or []
    address = addresses[0] if addresses else {}
    lines = address.get("line") or []

    telecoms = patient.get("telecom") or []
    work = [item for item in telecoms if item.get("use") == "work"]
    home = [item for item in telecoms if item.get("use") != "work"]

    def xtn(items: list[dict[str, Any]], use: str) -> str:
        if not items:
            return ""
        item = items[0]
        value = _escape(item.get("value", ""), seps)
        system = (item.get("system") or "phone").lower()
        if system == "email":
            return seps.component.join(["", "NET", "Internet", value])
        equipment = {"phone": "PH", "fax": "FX", "pager": "BP"}.get(system, "PH")
        return seps.component.join([value, "PRN" if use == "home" else "WPN", equipment])

    identifiers = patient.get("identifier") or []
    pid3 = seps.repetition.join(
        _identifier_field(item, seps, type_default="MR")
        for item in identifiers
    )

    marital_code = ""
    marital_text = ""
    marital = patient.get("maritalStatus") or {}
    marital_coding = (marital.get("coding") or [{}])[0]
    if marital_coding.get("system") == V3_MARITAL:
        marital_code = MARITAL_V3_TO_V2.get(marital_coding.get("code") or "", marital_coding.get("code") or "")
    elif marital_coding.get("code"):
        marital_code = marital_coding["code"]
    marital_text = marital.get("text") or marital_coding.get("display") or ""

    fields = [""] * 30
    fields[0] = "1"
    fields[2] = pid3
    fields[4] = _join_components([_escape(family, seps), _escape(given, seps), _escape(middle, seps), _escape(suffix, seps), _escape(prefix, seps)], seps)
    fields[6] = _escape(iso_to_ts(patient.get("birthDate"), date_only=True), seps)
    fields[7] = GENDER_FROM_FHIR.get(patient.get("gender") or "", "")
    fields[10] = _join_components([
        _escape(lines[0] if len(lines) > 0 else "", seps),
        _escape(lines[1] if len(lines) > 1 else "", seps),
        _escape(address.get("city", ""), seps),
        _escape(address.get("state", ""), seps),
        _escape(address.get("postalCode", ""), seps),
        _escape(address.get("country", ""), seps),
    ], seps)
    fields[12] = xtn(home, "home")
    fields[13] = xtn(work, "work")
    communication = patient.get("communication") or []
    if communication:
        language = (communication[0].get("language") or {})
        language_coding = (language.get("coding") or [{}])[0]
        language_field = _join_components([
            _escape(language_coding.get("code", ""), seps),
            _escape(language.get("text", ""), seps),
        ], seps)
    else:
        language_field = ""
    fields[14] = language_field
    fields[15] = _join_components([_escape(marital_code, seps), _escape(marital_text, seps)], seps)
    if patient.get("deceasedDateTime"):
        fields[28] = _escape(iso_to_ts(patient["deceasedDateTime"]), seps)
    elif patient.get("deceasedBoolean") is True:
        fields[29] = "Y"
    return [Segment("PID", fields, seps)]


def _build_encounter_segment(encounter: dict[str, Any], patient: dict[str, Any], practitioners: dict[str, dict[str, Any]], seps: Separators, ctx: MappingContext) -> Segment:
    fields = [""] * 45
    fields[0] = "1"
    class_code = (encounter.get("class") or {}).get("code") or ""
    fields[1] = CLASS_FHIR_TO_V2.get(class_code, "O")
    if class_code and class_code not in CLASS_FHIR_TO_V2:
        ctx.warn(f"Encounter.class {class_code!r} has no v2 patient class; used O")

    locations = encounter.get("location") or []
    display = ((locations[0].get("location") or {}).get("display") or "") if locations else ""
    fields[2] = _escape(display, seps)

    for role, field_number in (("ATND", 7), ("REF", 8), ("CON", 9), ("ADM", 17)):
        for participant in encounter.get("participant") or []:
            type_code = ((participant.get("type") or [{}])[0].get("coding") or [{}])[0].get("code")
            if type_code != role:
                continue
            reference = (participant.get("individual") or {}).get("reference") or ""
            practitioner = _lookup_practitioner(reference, practitioners)
            if practitioner and not fields[field_number - 1]:
                fields[field_number - 1] = _xcn_from_practitioner(practitioner, seps)

    identifiers = encounter.get("identifier") or []
    if identifiers:
        fields[18] = _identifier_field(identifiers[0], seps, type_default="VN")

    period = encounter.get("period") or {}
    fields[43] = _escape(iso_to_ts(period.get("start")), seps)
    fields[44] = _escape(iso_to_ts(period.get("end")), seps)

    service = encounter.get("serviceType") or {}
    fields[9] = _escape(service.get("text", ""), seps)
    return Segment("PV1", fields, seps)


def _build_obx(
    observation: dict[str, Any],
    sequence: int,
    seps: Separators,
    ctx: MappingContext,
    practitioners: dict[str, dict[str, Any]] | None = None,
) -> tuple[Segment, list[Segment]]:
    practitioners = practitioners or {}
    value_type = "ST"
    value = ""
    units = ""

    if "valueQuantity" in observation:
        quantity = observation["valueQuantity"] or {}
        value_type = "NM"
        value = _escape(_number_text(quantity.get("value", "")), seps) if quantity.get("value") is not None else ""
        unit_code = quantity.get("code") or ""
        unit_text = quantity.get("unit") or ""
        if unit_code:
            units = _join_components([_escape(unit_code, seps), _escape(unit_text, seps), "UCUM"], seps)
        elif unit_text:
            units = _join_components(["", _escape(unit_text, seps)], seps)
    elif "valueCodeableConcept" in observation:
        value_type = "CWE"
        value = concept_to_ce(observation["valueCodeableConcept"], seps)
    elif "valueDateTime" in observation:
        value_type = "DTM"
        value = _escape(iso_to_ts(observation["valueDateTime"]), seps)
    elif "valueTime" in observation:
        value_type = "TM"
        value = _escape(str(observation["valueTime"]).replace(":", ""), seps)
    elif "valueBoolean" in observation:
        value_type = "ST"
        value = "Y" if observation["valueBoolean"] else "N"
    elif "valueRange" in observation:
        value_type = "NR"
        low = (observation["valueRange"].get("low") or {}).get("value", "")
        high = (observation["valueRange"].get("high") or {}).get("value", "")
        if low != "" and high != "":
            value = _escape(low, seps) + seps.subcomponent + _escape(high, seps)
        else:
            value = _escape(str(low if low != "" else high), seps)
    elif "valueInteger" in observation:
        value_type = "NM"
        value = _escape(_number_text(observation["valueInteger"]), seps)
    elif "valueDecimal" in observation:
        value_type = "NM"
        value = _escape(_number_text(observation["valueDecimal"]), seps)
    else:
        value_type = "ST"
        value = _escape_free(observation.get("valueString", ""), seps)

    fields = [""] * 25
    fields[0] = str(sequence)
    fields[1] = value_type
    fields[2] = concept_to_ce(observation.get("code"), seps)
    fields[4] = value
    fields[5] = units
    reference_range = (observation.get("referenceRange") or [{}])[0]
    fields[6] = _escape(reference_range.get("text", ""), seps)
    fields[7] = seps.repetition.join(
        _escape(((item.get("coding") or [{}])[0]).get("code", ""), seps)
        for item in observation.get("interpretation") or []
    )
    fields[10] = RESULT_STATUS_FHIR_TO_V2.get(observation.get("status") or "", "P")
    fields[13] = _escape(iso_to_ts(observation.get("effectiveDateTime")), seps)
    fields[18] = _escape(iso_to_ts(observation.get("issued")), seps)
    performer_ref = ((observation.get("performer") or [{}])[0].get("reference") or "") if observation.get("performer") else ""
    fields[15] = _xcn_from_practitioner(_lookup_practitioner(performer_ref, practitioners), seps)
    fields[16] = concept_to_ce(observation.get("method"), seps)
    fields[19] = concept_to_ce(observation.get("bodySite"), seps)
    identifiers = observation.get("identifier") or []
    if identifiers:
        fields[20] = _ei_field(identifiers[0], seps)

    notes = [segment for segment in (
        Segment("NTE", ["1", "", _escape_free(note.get("text", ""), seps)], seps)
        for note in observation.get("note") or []
        if note.get("text")
    )]
    return Segment("OBX", fields, seps), notes


def _build_orc(service_request: dict[str, Any], practitioners: dict[str, dict[str, Any]], seps: Separators) -> Segment:
    fields = [""] * 20
    fields[0] = ORDER_STATUS_FHIR_TO_V2.get(service_request.get("status") or "", "NW")
    identifiers = service_request.get("identifier") or []
    if len(identifiers) > 0:
        fields[1] = _ei_field(identifiers[0], seps)
    if len(identifiers) > 1:
        fields[2] = _ei_field(identifiers[1], seps)
    fields[8] = _escape(iso_to_ts(service_request.get("authoredOn")), seps)
    requester = (service_request.get("requester") or {}).get("reference") or ""
    fields[11] = _xcn_from_practitioner(_lookup_practitioner(requester, practitioners), seps)
    return Segment("ORC", fields, seps)


def _build_obr(resource: dict[str, Any], practitioners: dict[str, dict[str, Any]], seps: Separators, *, order: bool = False) -> Segment:
    fields = [""] * 30
    fields[0] = "1"
    identifiers = resource.get("identifier") or []
    if order:
        if len(identifiers) > 0:
            fields[1] = _ei_field(identifiers[0], seps)
        if len(identifiers) > 1:
            fields[2] = _ei_field(identifiers[1], seps)
    else:
        if len(identifiers) > 0:
            fields[2] = _ei_field(identifiers[0], seps)
        if len(identifiers) > 1:
            fields[1] = _ei_field(identifiers[1], seps)
    fields[3] = concept_to_ce(resource.get("code"), seps)
    priority = resource.get("priority")
    if order and priority:
        fields[4] = _escape(PRIORITY_FHIR_TO_V2.get(priority, ""), seps)
    if order:
        fields[6] = _escape(iso_to_ts(resource.get("occurrenceDateTime")), seps)
    else:
        fields[6] = _escape(iso_to_ts(resource.get("effectiveDateTime")), seps)
        fields[21] = _escape(iso_to_ts(resource.get("issued")), seps)

    performer_ref = ((resource.get("performer") or [{}])[0].get("reference") or "") if resource.get("performer") else ""
    fields[15] = _xcn_from_practitioner(_lookup_practitioner(performer_ref, practitioners), seps)
    if not order:
        fields[24] = RESULT_STATUS_FHIR_TO_V2.get(resource.get("status") or "", "F")
    return Segment("OBR", fields, seps)


def _msh(code: str, trigger: str, seps: Separators, *, sending_app: str, sending_facility: str,
         receiving_app: str, receiving_facility: str, version: str, control_id: str, timestamp: datetime) -> Segment:
    fields = [seps.field, seps.encoding_characters]
    fields.extend([
        _escape(sending_app, seps),
        _escape(sending_facility, seps),
        _escape(receiving_app, seps),
        _escape(receiving_facility, seps),
        timestamp.strftime("%Y%m%d%H%M%S"),
        "",
        seps.component.join([_escape(code, seps), _escape(trigger, seps), _escape(f"{code}_{trigger}", seps)]),
        _escape(control_id, seps),
        "P",
        _escape(version, seps),
    ])
    return Segment("MSH", fields, seps)


def fhir_to_hl7(
    resources: dict[str, Any] | Sequence[dict[str, Any]],
    *,
    message_type: str | None = None,
    sending_app: str = "FHIR-APP",
    sending_facility: str = "FACILITY",
    receiving_app: str = "RECEIVING-APP",
    receiving_facility: str = "RECEIVING-FACILITY",
    version: str = "2.5.1",
    control_id: str | None = None,
    now: datetime | None = None,
    encoding: str = "utf-8",
) -> Message:
    grouped, by_url = _index(resources)
    patient = (grouped.get("Patient") or [None])[0]
    if patient is None:
        raise Hl7Error("no Patient resource: HL7 v2 messages built here require a patient context")

    if message_type:
        parts = message_type.split("^")
        if len(parts) != 2:
            raise Hl7Error("message_type must look like ADT^A08")
        code, trigger = parts[0].strip().upper(), parts[1].strip().upper()
    elif grouped.get("DocumentReference"):
        from hl7_siu_mdm import trigger_for_document

        code = "MDM"
        trigger = trigger_for_document((grouped.get("DocumentReference") or [None])[0])
    elif grouped.get("Appointment"):
        from hl7_siu_mdm import trigger_for_appointment

        code = "SIU"
        trigger = trigger_for_appointment((grouped.get("Appointment") or [None])[0])
    elif grouped.get("DiagnosticReport") or grouped.get("Observation"):
        code, trigger = "ORU", "R01"
    elif grouped.get("ServiceRequest"):
        code, trigger = "ORM", "O01"
    else:
        code, trigger = "ADT", "A08"

    seps = Separators()
    ctx = MappingContext(deterministic=True)
    stamp = now or datetime.now()
    cid = control_id or uuid.uuid4().hex.upper()[:20]

    practitioners: dict[str, dict[str, Any]] = {}
    for item in grouped.get("Practitioner", []):
        resource_id = item.get("id")
        if resource_id:
            practitioners[f"Practitioner/{resource_id}"] = item
    for url, item in by_url.items():
        if item.get("resourceType") == "Practitioner":
            practitioners[url] = item

    segments: list[Segment] = [
        _msh(
            code, trigger, seps,
            sending_app=sending_app, sending_facility=sending_facility,
            receiving_app=receiving_app, receiving_facility=receiving_facility,
            version=version, control_id=cid, timestamp=stamp,
        )
    ]

    patient_segments = _build_patient_segments(patient, seps, ctx)
    if code == "ADT":
        segments.append(Segment("EVN", [trigger, stamp.strftime("%Y%m%d%H%M%S")], seps))
        segments.extend(patient_segments)
    elif code != "SIU":
        # SIU places PID after SCH/TQ1; its branch inserts the segment at the right position.
        segments.extend(patient_segments)

    if code == "ADT":
        encounter = (grouped.get("Encounter") or [None])[0]
        if encounter:
            segments.append(_build_encounter_segment(encounter, patient, practitioners, seps, ctx))
        else:
            ctx.warn("ADT without Encounter: no PV1 segment produced")

    elif code == "ORU":
        report = (grouped.get("DiagnosticReport") or [None])[0]
        observations = grouped.get("Observation") or []
        if report is None and not observations:
            raise Hl7Error("ORU requires a DiagnosticReport or Observation")
        if report is None:
            report = {"resourceType": "DiagnosticReport", "status": "final", "code": {"text": "Observation results"}}
            ctx.warn("ORU without DiagnosticReport; OBR built from a text-only report")

        service_request = None
        for reference in report.get("basedOn") or []:
            ref = (reference or {}).get("reference") or ""
            candidate = by_url.get(ref.rstrip("/")) or by_url.get(ref.rstrip("/").rsplit("/", 1)[-1])
            if candidate and candidate.get("resourceType") == "ServiceRequest":
                service_request = candidate
                break
        if service_request:
            segments.append(_build_orc(service_request, practitioners, seps))
        segments.append(_build_obr(report, practitioners, seps))
        for note in report.get("note") or []:
            if note.get("text"):
                segments.append(Segment("NTE", ["1", "", _escape_free(note["text"], seps)], seps))
        for sequence, observation in enumerate(observations, start=1):
            obx, notes = _build_obx(observation, sequence, seps, ctx, practitioners)
            segments.append(obx)
            segments.extend(notes)

    elif code in {"ORM", "OML"}:
        request = (grouped.get("ServiceRequest") or [None])[0]
        if request is None:
            raise Hl7Error("ORM requires a ServiceRequest")
        segments.append(_build_orc(request, practitioners, seps))
        segments.append(_build_obr(request, practitioners, seps, order=True))
        for note in request.get("note") or []:
            if note.get("text"):
                segments.append(Segment("NTE", ["1", "", _escape_free(note["text"], seps)], seps))

    elif code == "SIU":
        from hl7_siu_mdm import SIU_STATUS_BY_TRIGGER, appointment_segments

        if trigger in {"S23", "S24", "S25"}:
            raise Hl7Error(f"SIU^{trigger} is a slot/schedule operation and cannot be built from an Appointment")
        appointment = (grouped.get("Appointment") or [None])[0]
        if appointment is None:
            raise Hl7Error("SIU requires an Appointment")
        status_override = SIU_STATUS_BY_TRIGGER.get(trigger)
        if status_override and appointment.get("status") and status_override != appointment["status"] and message_type:
            ctx.warn(
                f"message type SIU^{trigger} overrides Appointment.status "
                f"{appointment['status']!r} with {status_override!r}"
            )
        siu_segments = appointment_segments(
            appointment, by_url, seps, ctx, status_override=status_override
        )
        split = next((index for index, segment in enumerate(siu_segments) if segment.name == "RGS"), len(siu_segments))
        segments.extend(siu_segments[:split])
        segments.extend(patient_segments)
        segments.extend(siu_segments[split:])

    elif code == "MDM":
        from hl7_siu_mdm import document_segments

        document = (grouped.get("DocumentReference") or [None])[0]
        if document is None:
            raise Hl7Error("MDM requires a DocumentReference")

        encounter = None
        for reference in (document.get("context") or {}).get("encounter") or []:
            ref = (reference or {}).get("reference") or ""
            candidate = by_url.get(ref.rstrip("/")) or by_url.get(ref.rstrip("/").rsplit("/", 1)[-1])
            if candidate and candidate.get("resourceType") == "Encounter":
                encounter = candidate
                break
        if encounter is None:
            encounter = (grouped.get("Encounter") or [None])[0]
        if encounter:
            segments.append(_build_encounter_segment(encounter, patient, practitioners, seps, ctx))
        segments.extend(document_segments(document, practitioners, seps, ctx, trigger=trigger))

    message = Message(segments, seps, encoding)
    message.warnings.extend(ctx.warnings)
    return message


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _read_json(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _write_json(path: str | None, payload: Any) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if path:
        Path(path).write_text(text + "\n", encoding="utf-8")
        print(f"wrote {path}")
    else:
        print(text)


def cmd_hl7_to_fhir(args: argparse.Namespace) -> int:
    message = Message.parse(Path(args.file).read_bytes(), encoding=args.encoding or None, strict=False)
    bundle, warnings = hl7_to_fhir_report(
        message,
        default_patient_system=args.patient_system,
        bundle_type=args.bundle_type,
        deterministic=not args.random_uuids,
        include_provenance=args.provenance,
    )
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    _write_json(args.out, bundle)
    return 0


def cmd_fhir_to_hl7(args: argparse.Namespace) -> int:
    message = fhir_to_hl7(
        _read_json(args.file),
        message_type=args.type,
        sending_app=args.sending_app,
        sending_facility=args.sending_facility,
        receiving_app=args.receiving_app,
        receiving_facility=args.receiving_facility,
        version=args.version,
        control_id=args.control_id,
    )
    for warning in getattr(message, "warnings", []):
        print(f"warning: {warning}", file=sys.stderr)
    payload = message.to_er7()
    if args.out:
        Path(args.out).write_bytes(payload.encode(message.encoding))
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(payload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HL7 v2 <-> FHIR R4 mapper")
    sub = parser.add_subparsers(dest="command", required=True)

    to_fhir = sub.add_parser("hl7-to-fhir", help="map an HL7 v2 message to a FHIR Bundle")
    to_fhir.add_argument("file")
    to_fhir.add_argument("--out", default=None)
    to_fhir.add_argument("--bundle-type", default="collection", choices=["collection", "transaction"])
    to_fhir.add_argument("--patient-system", default=None, help="identifier system for PID-3 when the assigning authority is empty")
    to_fhir.add_argument("--random-uuids", action="store_true", help="use random fullUrls instead of deterministic uuid5")
    to_fhir.add_argument("--provenance", action="store_true", help="add a Provenance resource for sender/receiver")
    to_fhir.add_argument("--encoding", default=None)
    to_fhir.set_defaults(func=cmd_hl7_to_fhir)

    to_hl7 = sub.add_parser("fhir-to-hl7", help="map FHIR resources to an HL7 v2 message")
    to_hl7.add_argument("file")
    to_hl7.add_argument("--type", default=None, help="CODE^TRIGGER, e.g. ADT^A08; inferred when omitted")
    to_hl7.add_argument("--out", default=None)
    to_hl7.add_argument("--sending-app", default="FHIR-APP")
    to_hl7.add_argument("--sending-facility", default="FACILITY")
    to_hl7.add_argument("--receiving-app", default="RECEIVING-APP")
    to_hl7.add_argument("--receiving-facility", default="RECEIVING-FACILITY")
    to_hl7.add_argument("--version", default="2.5.1")
    to_hl7.add_argument("--control-id", default=None)
    to_hl7.set_defaults(func=cmd_fhir_to_hl7)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Hl7Error as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except FileNotFoundError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
