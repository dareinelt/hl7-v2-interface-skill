#!/usr/bin/env python3
"""SIU scheduling and MDM document mapping between HL7 v2.x and FHIR R4.

Used by `hl7_fhir_map` for the message families that need their own resource
shape: SIU -> Appointment and MDM -> DocumentReference. Both directions are
implemented; the mapping is conservative and reports every gap as a warning.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any, Sequence

from hl7core import Message, Segment, Separators, unescape_text
from hl7_fhir_map import (
    MappingContext,
    PARTICIPATION_DISPLAY,
    V2_0203,
    V3_PARTICIPATION,
    _ei_field,
    _escape,
    _escape_free,
    _identifier_from_ei,
    _join_components,
    _lookup_practitioner,
    _plain,
    _practitioner_from_xcn,
    _xcn_from_practitioner,
    codeable_concept,
    concept_to_ce,
    encounter_from_message,
    iso_to_ts,
    ts_to_iso,
)

V2_0270 = "http://terminology.hl7.org/CodeSystem/v2-0270"
V2_0271 = "http://terminology.hl7.org/CodeSystem/v2-0271"
V2_0272 = "http://terminology.hl7.org/CodeSystem/v2-0272"
V2_0278 = "http://terminology.hl7.org/CodeSystem/v2-0278"
V3_CONFIDENTIALITY = "http://terminology.hl7.org/CodeSystem/v3-Confidentiality"
DOC_REFERENCE_STATUS = "http://hl7.org/fhir/document-reference-status"

# SIU trigger events that describe an appointment (S23/S24 are slot operations,
# S25 belongs to the schedule query SQM/SQR pair).
SIU_STATUS_BY_TRIGGER = {
    "S12": "booked",
    "S13": "booked",
    "S14": "booked",
    "S15": "cancelled",
    "S16": "cancelled",
    "S17": "entered-in-error",
    "S18": "booked",
    "S19": "booked",
    "S20": "cancelled",
    "S21": "cancelled",
    "S22": "entered-in-error",
    "S26": "noshow",
}

SIU_STATUS_V2_TO_FHIR = {
    "BOOKED": "booked",
    "PENDING": "pending",
    "WAITLIST": "waitlist",
    "CANCELLED": "cancelled",
    "BLOCKED": "pending",
    "STARTED": "arrived",
    "COMPLETE": "fulfilled",
    "NOSHOW": "noshow",
    "DELETED": "entered-in-error",
}

SIU_STATUS_FHIR_TO_V2 = {
    "proposed": "PENDING",
    "pending": "PENDING",
    "waitlist": "WAITLIST",
    "booked": "BOOKED",
    "arrived": "STARTED",
    "checked-in": "STARTED",
    "fulfilled": "COMPLETE",
    "cancelled": "CANCELLED",
    "noshow": "NOSHOW",
    "entered-in-error": "DELETED",
}

PARTICIPANT_STATUS = {
    "booked": "accepted",
    "arrived": "accepted",
    "checked-in": "accepted",
    "fulfilled": "accepted",
    "pending": "tentative",
    "waitlist": "tentative",
    "proposed": "needs-action",
    "cancelled": "declined",
    "entered-in-error": "declined",
    "noshow": "accepted",
}

DOCUMENT_STATUS_BY_TRIGGER = {
    "T01": "current",
    "T02": "current",
    "T03": "current",
    "T04": "current",
    "T05": "current",
    "T06": "current",
    "T07": "current",
    "T08": "current",
    "T09": "current",
    "T10": "current",
    "T11": "entered-in-error",
}

CONTENT_TRIGGERS = {"T02", "T04", "T06", "T08", "T10"}

DOC_STATUS_V2_TO_FHIR = {
    "AU": "final",
    "LA": "final",
    "DO": "final",
    "DI": "preliminary",
    "IP": "preliminary",
    "IN": "preliminary",
    "PA": "preliminary",
}

DOC_STATUS_FHIR_TO_V2 = {
    "final": "AU",
    "amended": "AU",
    "preliminary": "IP",
    "entered-in-error": "AU",
}

DOC_STATUS_DISPLAY = {
    "AU": "Authenticated",
    "LA": "Legally authenticated",
    "DO": "Documented",
    "DI": "Dictated",
    "IP": "In progress",
    "IN": "Incomplete",
    "PA": "Pre-authenticated",
}

MIME_BY_SUBTYPE = {
    "PDF": "application/pdf",
    "TEXT": "text/plain",
    "TXT": "text/plain",
    "RTF": "application/rtf",
    "HTML": "text/html",
    "XML": "application/xml",
    "JPEG": "image/jpeg",
    "JPG": "image/jpeg",
    "PNG": "image/png",
    "TIFF": "image/tiff",
    "TIF": "image/tiff",
    "DICOM": "application/dicom",
    "CDA": "application/xml",
}

SUBTYPE_BY_MIME = {
    "application/pdf": "PDF",
    "text/plain": "TEXT",
    "text/html": "HTML",
    "application/rtf": "RTF",
    "application/xml": "XML",
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/tiff": "TIFF",
    "application/dicom": "DICOM",
}


# --------------------------------------------------------------------------
# SIU -> Appointment
# --------------------------------------------------------------------------

def _first_timestamp(message: Message, paths: Sequence[str]) -> str | None:
    for path in paths:
        segment_name, field_number = path.split("-")
        for segment in message.segments_named(segment_name):
            iso = ts_to_iso(segment.get(int(field_number)))
            if iso:
                return iso
    return None


def _duration_minutes(value: str, units: str, ctx: MappingContext, field: str) -> int | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        ctx.warn(f"{field}: duration {text!r} is not numeric; dropped")
        return None
    unit = (units or "").strip().lower()
    if unit in {"", "min", "minute", "minutes", "m"}:
        return int(round(number))
    if unit in {"h", "hr", "hour", "hours"}:
        return int(round(number * 60))
    if unit in {"s", "sec", "second", "seconds"}:
        return int(round(number / 60))
    ctx.warn(f"{field}: duration unit {units!r} is not mapped; dropped")
    return None


def _pl_display(raw: str, seps: Separators) -> str:
    components = [_plain(part, seps) for part in raw.split(seps.component)]
    parts = [
        components[index]
        for index in (3, 6, 7, 0, 1, 2)
        if len(components) > index and components[index]
    ]
    return " / ".join(parts)


def _extra_entry(ctx: MappingContext, message: Message, kind: str, key: str, resource: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
    reference = ctx.ref(message, kind, key)
    url = ctx.full_url(message, kind, key)
    return reference, {"fullUrl": url, "resource": resource}


def appointment_from_siu(
    message: Message,
    ctx: MappingContext,
    patient_ref: dict[str, str] | None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    seps = message.separators
    trigger = message.message_type[1]
    sch = message.segment("SCH")
    if trigger in {"S23", "S24", "S25"}:
        ctx.warn(f"SIU^{trigger} is a slot/schedule operation, not an appointment; no Appointment produced")
        return None, []
    if sch is None:
        ctx.warn("SIU without SCH; no Appointment resource produced")
        return None, []

    appointment: dict[str, Any] = {"resourceType": "Appointment"}

    identifiers = []
    seen_identifiers: set[tuple[str, str, str]] = set()
    identifier_types = {1: "PLAC", 2: "FILL", 4: ""}
    for number in (1, 2, 4):
        identifier = _identifier_from_ei(sch.get(number), seps)
        if identifier is None:
            continue
        type_code = identifier_types.get(number, "")
        if type_code:
            identifier["type"] = {"coding": [{"system": V2_0203, "code": type_code}]}
        else:
            identifier["type"] = {"text": "Placer group number"}
        key = (str(identifier.get("value", "")), str(identifier.get("system", "")), type_code)
        if key in seen_identifiers:
            continue
        seen_identifiers.add(key)
        identifiers.append(identifier)
    if identifiers:
        appointment["identifier"] = identifiers

    status_code = _plain(sch.get(25, comp=1), seps).upper()
    status = SIU_STATUS_V2_TO_FHIR.get(status_code) if status_code else None
    if status is None:
        status = SIU_STATUS_BY_TRIGGER.get(trigger, "booked")
        if status_code:
            ctx.warn(f"SCH-25 filler status {status_code!r} is not mapped; used {status} from {trigger}")
    appointment["status"] = status

    start = _first_timestamp(message, ("TQ1-7", "AIS-4", "AIG-8", "AIL-6", "AIP-6"))
    end = _first_timestamp(message, ("TQ1-8",))
    if sch.get(11):
        components = [_plain(part, seps) for part in sch.get(11).split(seps.component)]
        if not start and len(components) > 3:
            start = ts_to_iso(components[3])
        if not end and len(components) > 4:
            end = ts_to_iso(components[4])
    if start:
        appointment["start"] = start
    if end:
        appointment["end"] = end

    duration_value = _plain(sch.get(9), seps)
    duration_units = _plain(sch.get(10), seps)
    if not duration_value:
        for ais in message.segments_named("AIS"):
            if ais.get(7):
                duration_value = ais.get(7)
                duration_units = ais.get(8)
                break
    minutes = _duration_minutes(duration_value, duration_units, ctx, "SCH-9/AIS-7")
    if minutes is not None:
        appointment["minutesDuration"] = minutes

    service_types: list[dict[str, Any]] = []
    seen_services: set[str] = set()
    for ais in message.segments_named("AIS"):
        concept = codeable_concept(ais.get(3), seps)
        key = str((concept or {}).get("coding", [{}])[0].get("code") or (concept or {}).get("text") or "")
        if concept and key not in seen_services:
            seen_services.add(key)
            service_types.append(concept)
    if service_types:
        appointment["serviceType"] = service_types
    else:
        ctx.warn("SIU without AIS service type; Appointment.serviceType is empty")

    reasons = [
        concept
        for concept in (codeable_concept(sch.get(7), seps), codeable_concept(sch.get(6), seps))
        if concept
    ]
    if reasons:
        appointment["reasonCode"] = reasons
        for concept in reasons:
            text = concept.get("text") or ""
            if text:
                appointment["description"] = text
                break

    notes = [nte.get_text(3) for nte in message.segments_named("NTE") if nte.get_text(3)]
    if notes:
        appointment["comment"] = "\n".join(notes)

    participants: list[dict[str, Any]] = []
    participant_status = PARTICIPANT_STATUS.get(status, "accepted")

    def participant(reference: dict[str, str], role: str) -> dict[str, Any]:
        return {
            "actor": reference,
            "status": participant_status,
            "type": [{"coding": [{"system": V3_PARTICIPATION, "code": role}]}],
        }

    if patient_ref:
        participants.append(participant(patient_ref, "SBJ"))

    extras: list[dict[str, Any]] = []

    for aip in message.segments_named("AIP"):
        role_code = _plain(aip.get(4, comp=1), seps).upper() or "PART"
        if role_code not in {"ATND", "REF", "CON", "ADM", "PART", "PPRF", "SPRF"}:
            ctx.warn(f"AIP-4 provider role {role_code!r} is not a v3 participation code; used PART")
            role_code = "PART"
        for rep in aip.repetitions(3):
            parsed = _practitioner_from_xcn(rep, seps, ctx, "AIP-3")
            if parsed is None:
                continue
            key, practitioner = parsed
            reference, entry = _extra_entry(ctx, message, "Practitioner", key, practitioner)
            extras.append(entry)
            participants.append(participant(reference, role_code))

    for ail in message.segments_named("AIL"):
        raw_location = ail.get(3)
        display = _pl_display(raw_location, seps)
        if not display:
            continue
        location: dict[str, Any] = {"resourceType": "Location", "name": display}
        if raw_location:
            location["identifier"] = [{"value": _plain(raw_location, seps)}]
        reference, entry = _extra_entry(ctx, message, "Location", display, location)
        extras.append(entry)
        participants.append(participant(reference, "LOC"))

    for aig in message.segments_named("AIG"):
        concept = codeable_concept(aig.get(3), seps)
        code = ""
        name = ""
        if concept:
            coding = (concept.get("coding") or [{}])[0]
            code = coding.get("code") or ""
            name = coding.get("display") or concept.get("text") or code
        if not name:
            continue
        device: dict[str, Any] = {
            "resourceType": "Device",
            "deviceName": [{"name": name, "type": "user-friendly-name"}],
        }
        if code:
            device["identifier"] = [{"value": code}]
        reference, entry = _extra_entry(ctx, message, "Device", code or name, device)
        extras.append(entry)
        participants.append(participant(reference, "PPRF"))

    if participants:
        appointment["participant"] = participants

    return appointment, extras


# --------------------------------------------------------------------------
# MDM -> DocumentReference
# --------------------------------------------------------------------------

def _mime_type(type_of_data: str, subtype: str, ctx: MappingContext) -> str:
    candidate = subtype.strip()
    if "/" in candidate:
        return candidate
    mime = MIME_BY_SUBTYPE.get(candidate.upper())
    if mime:
        return mime
    if candidate:
        ctx.warn(f"OBX-5 ED subtype {candidate!r} has no MIME mapping; used application/octet-stream")
    return "application/octet-stream"


def attachment_from_obx(obx: Segment, seps: Separators, ctx: MappingContext) -> dict[str, Any] | None:
    raw = obx.get(5)
    if not raw:
        return None
    raw_components = raw.split(seps.component)
    if len(raw_components) < 5:
        ctx.warn("OBX-5 is not an ED value (fewer than 5 components); document content skipped")
        return None
    components = [unescape_text(part, seps) for part in raw_components]
    subtype = components[2].strip()
    encoding = components[3].strip().upper()
    data = components[4]

    if not data:
        return None

    try:
        if encoding == "BASE64":
            raw_bytes = base64.b64decode(data, validate=False)
            data_b64 = base64.b64encode(raw_bytes).decode("ascii")
        elif encoding == "HEX":
            raw_bytes = bytes.fromhex(data)
            data_b64 = base64.b64encode(raw_bytes).decode("ascii")
        else:
            raw_bytes = data.encode("latin-1", "replace")
            data_b64 = base64.b64encode(raw_bytes).decode("ascii")
    except (binascii.Error, ValueError) as error:
        ctx.warn(f"OBX-5 ED content cannot be decoded ({error}); document content skipped")
        return None

    attachment: dict[str, Any] = {
        "contentType": _mime_type(components[1], subtype, ctx),
        "data": data_b64,
        "size": len(raw_bytes),
    }
    created = ts_to_iso(obx.get(14))
    if created:
        attachment["creation"] = created
    return attachment


def document_from_mdm(
    message: Message,
    ctx: MappingContext,
    patient_ref: dict[str, str] | None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, Any] | None]:
    seps = message.separators
    trigger = message.message_type[1]
    txa = message.segment("TXA")
    if txa is None:
        ctx.warn("MDM without TXA; no DocumentReference resource produced")
        return None, [], None

    document: dict[str, Any] = {
        "resourceType": "DocumentReference",
        "status": DOCUMENT_STATUS_BY_TRIGGER.get(trigger, "current"),
    }

    completion = _plain(txa.get(17), seps).upper()
    doc_status = DOC_STATUS_V2_TO_FHIR.get(completion)
    if doc_status:
        document["docStatus"] = {
            "coding": [{
                "system": DOC_REFERENCE_STATUS,
                "code": doc_status,
                "display": doc_status.title(),
            }],
            "text": f"{completion} {DOC_STATUS_DISPLAY.get(completion, completion)}",
        }
    elif completion:
        ctx.warn(f"TXA-17 completion status {completion!r} is not mapped; docStatus omitted")
    else:
        ctx.warn("TXA-17 completion status is empty; docStatus omitted")

    identifier = _identifier_from_ei(txa.get(12), seps)
    identifiers: list[dict[str, Any]] = []
    if identifier:
        document["masterIdentifier"] = identifier
        identifiers.append(identifier)
    else:
        ctx.warn("TXA-12 unique document number is empty; document has no identifier")
    for field_number, type_code in ((14, "PLAC"), (15, "FILL")):
        related = _identifier_from_ei(txa.get(field_number), seps)
        if related:
            related["type"] = {"coding": [{"system": V2_0203, "code": type_code}]}
            identifiers.append(related)
    if identifiers:
        document["identifier"] = identifiers

    doc_type = codeable_concept(txa.get(2), seps)
    type_text = ""
    if doc_type:
        document["type"] = doc_type
        coding = (doc_type.get("coding") or [{}])[0]
        type_text = coding.get("display") or doc_type.get("text") or ""
        if type_text:
            document["description"] = type_text
    else:
        ctx.warn("TXA-2 document type is empty; DocumentReference.type omitted")

    notes = [nte.get_text(3) for nte in message.segments_named("NTE") if nte.get_text(3)]
    if notes:
        # DocumentReference has no note element; description is the agreed local slot.
        document["description"] = "\n".join(notes)

    date = ts_to_iso(txa.get(4)) or ts_to_iso(txa.get(6))
    if date:
        document["date"] = date
    if patient_ref:
        document["subject"] = patient_ref

    extras: list[dict[str, Any]] = []
    authors: list[dict[str, str]] = []
    seen_authors: set[str] = set()
    for field_number in (9, 5):
        for rep in txa.repetitions(field_number):
            parsed = _practitioner_from_xcn(rep, seps, ctx, f"TXA-{field_number}")
            if parsed is None:
                continue
            key, practitioner = parsed
            reference, entry = _extra_entry(ctx, message, "Practitioner", key, practitioner)
            if key not in seen_authors:
                seen_authors.add(key)
                extras.append(entry)
                authors.append(reference)
    if authors:
        document["author"] = authors

    authenticator = None
    for rep in txa.repetitions(10):
        parsed = _practitioner_from_xcn(rep, seps, ctx, "TXA-10")
        if parsed is None:
            continue
        key, practitioner = parsed
        reference, entry = _extra_entry(ctx, message, "Practitioner", key, practitioner)
        extras.append(entry)
        authenticator = reference
        break
    if authenticator:
        document["authenticator"] = authenticator

    security = codeable_concept(txa.get(18), seps)
    if security:
        coding = (security.get("coding") or [{}])[0]
        code = (coding.get("code") or "").upper()
        if code in {"N", "R", "V", "U"}:
            document["securityLabel"] = [{
                "coding": [{
                    "system": V3_CONFIDENTIALITY,
                    "code": code,
                    "display": coding.get("display") or code,
                }],
                "text": security.get("text") or coding.get("display") or code,
            }]
        else:
            document["securityLabel"] = [security]
            if code:
                ctx.warn(f"TXA-18 confidentiality code {code!r} has no v3-Confidentiality equivalent")

    parent = _identifier_from_ei(txa.get(13), seps)
    if parent:
        relation = None
        if trigger in {"T05", "T06"}:
            relation = "appends"
        elif trigger in {"T09", "T10"}:
            relation = "replaces"
        if relation:
            document["relatesTo"] = [{"code": relation, "target": {"identifier": parent}}]
        else:
            ctx.warn("TXA-13 parent document number is present but the trigger has no relation; ignored")

    content: list[dict[str, Any]] = []
    for obx in message.segments_named("OBX"):
        obx_type = _plain(obx.get(2), seps).upper()
        if obx_type != "ED":
            if obx.get(5):
                ctx.warn(f"OBX-2 {obx_type or '(empty)'} in MDM is not ED; document content skipped")
            continue
        attachment = attachment_from_obx(obx, seps, ctx)
        if attachment:
            content.append({"attachment": attachment})
    if content:
        document["content"] = content
    elif trigger in CONTENT_TRIGGERS:
        ctx.warn(f"MDM^{trigger} should carry document content, but no ED OBX was usable")

    encounter = None
    if message.segment("PV1"):
        encounter, encounter_practitioners = encounter_from_message(message, ctx, patient_ref)
        if encounter is not None:
            encounter["status"] = "in-progress"
            encounter_url = ctx.full_url(message, "Encounter", str(message.get("PV1-19") or message.control_id))
            document.setdefault("context", {})["encounter"] = [{"reference": encounter_url}]
            for practitioner in encounter_practitioners:
                key = (practitioner.get("identifier") or [{}])[0].get("value") or "practitioner"
                reference, entry = _extra_entry(ctx, message, "Practitioner", key, practitioner)
                if not any(item["fullUrl"] == entry["fullUrl"] for item in extras):
                    extras.append(entry)

    return document, extras, encounter


# --------------------------------------------------------------------------
# FHIR -> SIU
# --------------------------------------------------------------------------

def trigger_for_appointment(appointment: dict[str, Any] | None) -> str:
    if not appointment:
        return "S12"
    status = appointment.get("status")
    return {"cancelled": "S15", "noshow": "S26", "entered-in-error": "S17"}.get(status, "S12")


def _appointment_status_code(status: str | None) -> str:
    return SIU_STATUS_FHIR_TO_V2.get(status or "", "BOOKED")


def _coded(status_code: str, seps: Separators) -> str:
    return _join_components([_escape(status_code, seps), _escape(status_code.title(), seps), _escape("HL70278", seps)], seps)


def _duration_fields(minutes: Any) -> tuple[str, str]:
    if minutes is None:
        return "", ""
    try:
        value = int(minutes)
    except (TypeError, ValueError):
        return "", ""
    return str(value), "min"


def _identifier_by_type(identifiers: Sequence[dict[str, Any]], type_code: str) -> dict[str, Any] | None:
    for identifier in identifiers:
        coding = ((identifier.get("type") or {}).get("coding") or [{}])[0]
        if coding.get("code") == type_code:
            return identifier
    return None


def appointment_segments(
    appointment: dict[str, Any],
    by_url: dict[str, dict[str, Any]],
    seps: Separators,
    ctx: MappingContext,
    *,
    status_override: str | None = None,
) -> list[Segment]:
    status_code = _appointment_status_code(status_override or appointment.get("status"))
    identifiers = appointment.get("identifier") or []
    start = iso_to_ts(appointment.get("start"))
    end = iso_to_ts(appointment.get("end"))
    duration_value, duration_units = _duration_fields(appointment.get("minutesDuration"))

    participants: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for participant in appointment.get("participant") or []:
        reference = (participant.get("actor") or {}).get("reference") or ""
        resource = by_url.get(reference.rstrip("/")) or by_url.get(reference.rstrip("/").rsplit("/", 1)[-1])
        if resource:
            participants.append((participant, resource))

    practitioners = [resource for _, resource in participants if resource.get("resourceType") == "Practitioner"]
    contact = _xcn_from_practitioner(practitioners[0], seps) if practitioners else ""

    placer = _identifier_by_type(identifiers, "PLAC")
    filler = _identifier_by_type(identifiers, "FILL")
    others = [item for item in identifiers if item is not placer and item is not filler]
    if placer is None and others:
        placer = others.pop(0)
    if filler is None and others:
        filler = others.pop(0)
    group = others[0] if others else None

    sch = [""] * 27
    sch[0] = _ei_field(placer, seps) if placer else ""
    sch[1] = _ei_field(filler, seps) if filler else ""
    sch[3] = _ei_field(group, seps) if group else ""
    reasons = appointment.get("reasonCode") or []
    sch[5] = concept_to_ce(reasons[1] if len(reasons) > 1 else None, seps)
    sch[6] = concept_to_ce(reasons[0] if reasons else None, seps)
    sch[8] = _escape(duration_value, seps)
    sch[9] = _escape(duration_units, seps)
    if start or end:
        sch[10] = _join_components(["", "", "", _escape(start, seps), _escape(end, seps)], seps)
    sch[11] = contact
    sch[24] = _coded(status_code, seps)

    segments: list[Segment] = [Segment("SCH", sch, seps)]

    if start or end:
        tq1 = [""] * 8
        tq1[0] = "1"
        tq1[6] = _escape(start, seps)
        tq1[7] = _escape(end, seps)
        segments.append(Segment("TQ1", tq1, seps))

    segments.append(Segment("RGS", ["1", "A"], seps))

    for sequence, service in enumerate(appointment.get("serviceType") or [], start=1):
        ais = [""] * 10
        ais[0] = str(sequence)
        ais[1] = "A"
        ais[2] = concept_to_ce(service, seps)
        ais[3] = _escape(start, seps)
        ais[6] = _escape(duration_value, seps)
        ais[7] = _escape(duration_units, seps)
        ais[9] = _coded(status_code, seps)
        segments.append(Segment("AIS", ais, seps))

    aip_sequence = 0
    ail_sequence = 0
    aig_sequence = 0
    for participant, resource in participants:
        resource_type = resource.get("resourceType")
        if resource_type == "Practitioner":
            aip_sequence += 1
            aip = [""] * 12
            aip[0] = str(aip_sequence)
            aip[1] = "A"
            aip[2] = _xcn_from_practitioner(resource, seps)
            role_code = (((participant.get("type") or [{}])[0].get("coding") or [{}])[0].get("code") or "PART").upper()
            aip[3] = _join_components([_escape(role_code, seps), _escape(PARTICIPATION_DISPLAY.get(role_code, role_code), seps)], seps)
            aip[5] = _escape(start, seps)
            aip[11] = _coded(status_code, seps)
            segments.append(Segment("AIP", aip, seps))
        elif resource_type == "Location":
            ail_sequence += 1
            ail = [""] * 12
            ail[0] = str(ail_sequence)
            ail[1] = "A"
            raw_location = str((resource.get("identifier") or [{}])[0].get("value") or resource.get("name", ""))
            ail[2] = seps.component.join(_escape(part, seps) for part in raw_location.split(seps.component))
            ail[3] = ""
            ail[5] = _escape(start, seps)
            ail[11] = _coded(status_code, seps)
            segments.append(Segment("AIL", ail, seps))
        elif resource_type == "Device":
            aig_sequence += 1
            code = str((resource.get("identifier") or [{}])[0].get("value") or "")
            name = ((resource.get("deviceName") or [{}])[0]).get("name", "")
            aig = [""] * 14
            aig[0] = str(aig_sequence)
            aig[1] = "A"
            aig[2] = _join_components([_escape(code, seps), _escape(name, seps)], seps)
            aig[3] = _join_components([_escape("EQP", seps), _escape("Equipment", seps)], seps)
            aig[5] = _escape(start, seps)
            aig[13] = _coded(status_code, seps)
            segments.append(Segment("AIG", aig, seps))

    comment = appointment.get("comment")
    if comment:
        segments.append(Segment("NTE", ["1", "", _escape_free(comment, seps)], seps))
    return segments


# --------------------------------------------------------------------------
# FHIR -> MDM
# --------------------------------------------------------------------------

def trigger_for_document(document: dict[str, Any] | None) -> str:
    if not document:
        return "T01"
    if document.get("status") == "entered-in-error":
        return "T11"
    relates = (((document.get("relatesTo") or [{}])[0]).get("code") or "").lower()
    if relates == "appends":
        return "T06"
    if relates == "replaces":
        return "T10"
    attachment = (((document.get("content") or [{}])[0]).get("attachment") or {})
    return "T02" if attachment.get("data") else "T01"


def _presentation(content_type: str) -> str:
    value = (content_type or "").lower()
    if value.startswith("text/plain"):
        return "TX"
    if value.startswith("text/html") or value.startswith("application/rtf"):
        return "FT"
    return "ED"


def _ed_subtype(content_type: str) -> str:
    value = (content_type or "").split(";", 1)[0].strip().lower()
    return SUBTYPE_BY_MIME.get(value, "")


def document_segments(
    document: dict[str, Any],
    practitioners: dict[str, dict[str, Any]],
    seps: Separators,
    ctx: MappingContext,
    *,
    trigger: str | None = None,
) -> list[Segment]:
    txa = [""] * 23
    txa[0] = "1"
    txa[1] = concept_to_ce(document.get("type"), seps)

    attachment = (((document.get("content") or [{}])[0]).get("attachment") or {})
    content_type = str(attachment.get("contentType") or "")
    txa[2] = _presentation(content_type) if content_type else ""

    date = iso_to_ts(document.get("date"))
    txa[3] = _escape(date, seps)
    txa[5] = _escape(date, seps)

    author_ref = ""
    if document.get("author"):
        author_ref = (document["author"][0] or {}).get("reference") or ""
    author = _xcn_from_practitioner(_lookup_practitioner(author_ref, practitioners), seps)
    txa[4] = author
    txa[8] = author

    authenticator_ref = (document.get("authenticator") or {}).get("reference") or ""
    txa[9] = _xcn_from_practitioner(_lookup_practitioner(authenticator_ref, practitioners), seps)

    identifier = document.get("masterIdentifier") or ((document.get("identifier") or [None])[0])
    if identifier:
        txa[11] = _ei_field(identifier, seps)
    for related in document.get("identifier") or []:
        type_code = ((related.get("type") or {}).get("coding") or [{}])[0].get("code")
        if type_code == "PLAC":
            txa[13] = _ei_field(related, seps)
        elif type_code == "FILL":
            txa[14] = _ei_field(related, seps)

    relates = (document.get("relatesTo") or [None])[0]
    if relates:
        parent = ((relates.get("target") or {}).get("identifier")) or None
        if parent:
            txa[12] = _ei_field(parent, seps)

    doc_status_concept = document.get("docStatus") or {}
    coding_code = ((doc_status_concept.get("coding") or [{}])[0]).get("code")
    status_text = str(doc_status_concept.get("text") or "")
    v2_code = status_text.split()[0].upper() if status_text else ""
    if v2_code in DOC_STATUS_V2_TO_FHIR:
        txa[16] = v2_code
    elif coding_code in DOC_STATUS_V2_TO_FHIR:
        txa[16] = coding_code
    else:
        fhir_status = str(coding_code or status_text).lower()
        txa[16] = DOC_STATUS_FHIR_TO_V2.get(fhir_status, "AU")

    security = (document.get("securityLabel") or [None])[0]
    txa[17] = concept_to_ce(security, seps)
    txa[18] = "AV"
    txa[19] = "AA"

    segments: list[Segment] = [Segment("TXA", txa, seps)]

    if attachment.get("data") and trigger not in {None, "T02", "T04", "T06", "T08", "T10"}:
        ctx.warn(f"MDM^{trigger} is notification-only; attachment data omitted")
    elif attachment.get("data"):
        obx = [""] * 15
        obx[0] = "1"
        obx[1] = "ED"
        obx[2] = concept_to_ce(document.get("type"), seps)
        obx[3] = "1"
        subtype = _ed_subtype(content_type)
        if not subtype:
            ctx.warn(f"content type {content_type!r} has no ED subtype; used BINARY")
            subtype = "BINARY"
        obx[4] = _join_components(
            ["", _escape("Application", seps), _escape(subtype, seps), "Base64", str(attachment["data"])],
            seps,
        )
        obx[10] = "F"
        obx[13] = _escape(date, seps)
        segments.append(Segment("OBX", obx, seps))

    description = document.get("description")
    type_coding = ((document.get("type") or {}).get("coding") or [{}])[0]
    type_text = type_coding.get("display") or (document.get("type") or {}).get("text") or ""
    if description and description != type_text:
        segments.append(Segment("NTE", ["1", "", _escape_free(description, seps)], seps))
    return segments
