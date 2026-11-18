#!/usr/bin/env python3
"""German GDT/xDT <-> FHIR R4 and HL7 v2 bridge.

GDT parsing and writing is delegated to the sibling `gdt-interface` skill
(`scripts/gdt_tool.py`). This skill contributes the mapping to FHIR and the
composition GDT -> FHIR -> HL7 (and back), so all three formats share one
canonical model.

Commands:
  gdt-to-fhir  FILE.gdt  [--out bundle.json]
  fhir-to-gdt  FILE.json [--build OUT.gdt]
  gdt-to-hl7   FILE.gdt  [--out message.hl7]
  hl7-to-gdt   FILE.hl7  [--build OUT.gdt]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import uuid
from datetime import date
from pathlib import Path
from typing import Any, Sequence

HERE = Path(__file__).resolve()
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

from hl7core import Hl7Error, Message  # noqa: E402
from hl7_fhir_map import DEFAULT_NAMESPACE, fhir_to_hl7, hl7_to_fhir_report  # noqa: E402

PATIENT_FIELDS = ("3000", "3101", "3102", "3103", "3105", "3106", "3107", "3110", "3618", "3619")
RESULT_FIELDS = ("8402", "8420", "6228")

SATZART_NAMES = {
    "6301": "Patientenstammdaten",
    "6302": "Untersuchungsanforderung",
    "6310": "Untersuchungsergebnis",
}

HL7_FOR_SATZART = {"6301": "ADT^A04", "6302": "ORM^O01", "6310": "ORU^R01"}
SATZART_FOR_HL7 = {"ADT": "6301", "ORM": "6302", "OML": "6302", "ORU": "6310"}


def load_gdt_tool(explicit: str | None = None):
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get("GDT_TOOL"):
        candidates.append(Path(os.environ["GDT_TOOL"]))
    candidates.append(HERE.parents[2] / "gdt-interface" / "scripts" / "gdt_tool.py")
    candidates.append(Path.home() / ".copilot" / "skills" / "gdt-interface" / "scripts" / "gdt_tool.py")
    for candidate in candidates:
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location("gdt_tool", candidate)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules["gdt_tool"] = module
            spec.loader.exec_module(module)
            return module
    raise Hl7Error(
        "gdt_tool.py not found. Install the gdt-interface skill next to this one "
        "or pass --gdt-tool PATH."
    )


# --------------------------------------------------------------------------
# GDT helpers
# --------------------------------------------------------------------------

def parse_gdt_date(value: str, *, order: str = "auto") -> date | None:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) != 8:
        return None
    if order == "ddmmyyyy":
        day, month, year = digits[:2], digits[2:4], digits[4:]
    elif order == "yyyymmdd":
        year, month, day = digits[:4], digits[4:6], digits[6:]
    else:
        if 1900 <= int(digits[:4]) <= 2100:
            year, month, day = digits[:4], digits[4:6], digits[6:]
        else:
            day, month, year = digits[:2], digits[2:4], digits[4:]
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def format_gdt_date(value: str, *, order: str) -> str:
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(value or ""))
    if not match:
        return ""
    year, month, day = match.groups()
    return f"{day}{month}{year}" if order == "ddmmyyyy" else f"{year}{month}{day}"


def parse_gdt_number(value: str) -> tuple[float | None, str]:
    text = (value or "").strip()
    if not text:
        return None, ""
    normalized = text.replace(",", ".") if "," in text and "." not in text else text
    try:
        return float(normalized), text
    except ValueError:
        return None, text


def _full(kind: str, key: str) -> str:
    return f"urn:uuid:{uuid.uuid5(DEFAULT_NAMESPACE, f'gdt|{kind}|{key}')}"


def _gender(code: str) -> str | None:
    return {"1": "male", "2": "female"}.get(code.strip())


def _device(value: str, kind: str, key: str) -> tuple[str, dict[str, Any]] | None:
    if not value:
        return None
    return _full(kind, key), {
        "resourceType": "Device",
        "identifier": [{"value": value}],
        "deviceName": [{"name": value, "type": "user-friendly-name"}],
    }


def _patient_from_records(records: Sequence[Any], *, patient_system: str | None, insurance_system: str | None, warnings: list[str]) -> tuple[dict[str, Any] | None, str | None]:
    primary = next((record for record in records if record.get("3000")), None)
    if primary is None:
        warnings.append("no record with field 3000 (patient number); no Patient resource produced")
        return None, None
    value = primary.get("3000").strip()
    if not value:
        return None, None

    patient: dict[str, Any] = {"resourceType": "Patient"}
    identifier: dict[str, Any] = {"value": value}
    if patient_system:
        identifier["system"] = patient_system
    identifiers = [identifier]

    insurance = primary.get("3105").strip()
    if insurance:
        insurance_identifier: dict[str, Any] = {"value": insurance}
        if insurance_system:
            insurance_identifier["system"] = insurance_system
        identifiers.append(insurance_identifier)
    patient["identifier"] = identifiers

    family = primary.get("3101").strip()
    given = primary.get("3102").strip()
    if family or given:
        name: dict[str, Any] = {"use": "official"}
        if family:
            name["family"] = family
        if given:
            name["given"] = [given]
        patient["name"] = [name]
    else:
        warnings.append("GDT 3101/3102 are empty; Patient has no name")

    birth = parse_gdt_date(primary.get("3103"))
    if birth:
        patient["birthDate"] = birth.isoformat()
    elif primary.get("3103").strip():
        warnings.append(f"GDT 3103 {primary.get('3103')!r} is not a valid date; dropped")

    gender = _gender(primary.get("3110"))
    if gender:
        patient["gender"] = gender
    elif primary.get("3110").strip():
        warnings.append(f"GDT 3110 {primary.get('3110')!r} is not 1 or 2; mapped to unknown")
        patient["gender"] = "unknown"

    address: dict[str, Any] = {}
    city = primary.get("3106").strip()
    street = primary.get("3107").strip()
    if street:
        address["line"] = [street]
    if city:
        address["city"] = city
    if address:
        patient["address"] = [address]

    telecom: list[dict[str, Any]] = []
    phone = primary.get("3618").strip()
    email = primary.get("3619").strip()
    if phone:
        telecom.append({"system": "phone", "value": phone, "use": "home"})
    if email:
        telecom.append({"system": "email", "value": email, "use": "home"})
    if telecom:
        patient["telecom"] = telecom
    return patient, value


# --------------------------------------------------------------------------
# GDT -> FHIR
# --------------------------------------------------------------------------

def gdt_to_fhir_records(
    records: Sequence[Any],
    *,
    patient_system: str | None = None,
    insurance_system: str | None = None,
    unit: str | None = None,
    unit_code: str | None = None,
    provenance: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    patient, patient_number = _patient_from_records(
        records, patient_system=patient_system, insurance_system=insurance_system, warnings=warnings
    )
    entries: list[dict[str, Any]] = []
    patient_ref: dict[str, str] | None = None

    if patient and patient_number:
        url = _full("Patient", patient_number)
        patient_ref = {"reference": url}
        entries.append({"fullUrl": url, "resource": patient})

    devices: list[dict[str, Any]] = []
    device_refs: dict[str, str] = {}
    for field, kind in (("8316", "sender"), ("8315", "receiver")):
        value = next((record.get(field).strip() for record in records if record.get(field)), "")
        device = _device(value, "Device", f"{kind}|{value}")
        if device:
            url, resource = device
            device_refs[kind] = url
            devices.append({"fullUrl": url, "resource": resource})

    sender_ref = {"reference": device_refs["sender"]} if "sender" in device_refs else None
    receiver_ref = {"reference": device_refs["receiver"]} if "receiver" in device_refs else None

    patient_entry = (
        [{"fullUrl": _full("Patient", patient_number), "resource": patient}]
        if patient is not None and patient_number
        else []
    )
    entries = patient_entry + devices

    for index, record in enumerate(records, start=1):
        satzart = record.satzart
        if satzart == "6302":
            request: dict[str, Any] = {
                "resourceType": "ServiceRequest",
                "status": "active",
                "intent": "order",
            }
            if patient_ref:
                request["subject"] = patient_ref
            code_value = record.get("8402").strip()
            request["code"] = {"coding": [{"code": code_value}]} if code_value else {"text": "GDT order"}
            if not code_value:
                warnings.append(f"6302 record {index}: field 8402 is empty; used a text-only code")
            authored = parse_gdt_date(record.get("6200"))
            if authored:
                request["authoredOn"] = authored.isoformat()
            elif record.get("6200").strip():
                warnings.append(f"6302 record {index}: field 6200 is not a valid date; dropped")
            if sender_ref:
                request["requester"] = sender_ref
            if receiver_ref:
                request["performer"] = [receiver_ref]
            notes = [text for text in record.get_all("6228") if text]
            if notes:
                request["note"] = [{"text": "\n".join(notes)}]
            url = _full("ServiceRequest", f"{patient_number or 'unknown'}|{index}|{code_value}")
            entries.append({"fullUrl": url, "resource": request})

        elif satzart == "6310":
            report: dict[str, Any] = {"resourceType": "DiagnosticReport", "status": "final"}
            if patient_ref:
                report["subject"] = patient_ref
            code_value = record.get("8402").strip()
            report["code"] = {"coding": [{"code": code_value}]} if code_value else {"text": "GDT result"}
            if not code_value:
                warnings.append(f"6310 record {index}: field 8402 is empty; used a text-only code")
            effective = parse_gdt_date(record.get("6200"))
            if effective:
                report["effectiveDateTime"] = effective.isoformat()
            elif record.get("6200").strip():
                warnings.append(f"6310 record {index}: field 6200 is not a valid date; dropped")
            findings = [text for text in record.get_all("6228") if text]
            if findings:
                report["conclusion"] = "\n".join(findings)
            if receiver_ref:
                report["performer"] = [receiver_ref]

            observation_entries: list[dict[str, Any]] = []
            result_refs: list[dict[str, str]] = []
            for sequence, raw in enumerate(record.get_all("8420"), start=1):
                number, raw_text = parse_gdt_number(raw)
                observation: dict[str, Any] = {
                    "resourceType": "Observation",
                    "status": "final",
                    "code": {"coding": [{"code": code_value}]} if code_value else {"text": "GDT measurement"},
                }
                if patient_ref:
                    observation["subject"] = patient_ref
                if effective:
                    observation["effectiveDateTime"] = effective.isoformat()
                if number is None:
                    observation["valueString"] = raw_text
                    warnings.append(f"6310 record {index}: field 8420 {raw_text!r} is not numeric; kept as string")
                elif unit:
                    quantity: dict[str, Any] = {"value": number, "unit": unit}
                    if unit_code:
                        quantity["code"] = unit_code
                    observation["valueQuantity"] = quantity
                else:
                    observation["valueString"] = raw_text
                    warnings.append(
                        f"6310 record {index}: field 8420 has no unit in the record; "
                        "kept as valueString (pass --unit/--unit-code to emit a Quantity)"
                    )
                observation_url = _full("Observation", f"{patient_number or 'unknown'}|{index}|{sequence}")
                observation_entries.append({"fullUrl": observation_url, "resource": observation})
                result_refs.append({"reference": observation_url})

            if result_refs:
                report["result"] = result_refs
            report_url = _full("DiagnosticReport", f"{patient_number or 'unknown'}|{index}|{code_value}")
            entries.append({"fullUrl": report_url, "resource": report})
            entries.extend(observation_entries)

        elif satzart == "6301":
            continue
        else:
            warnings.append(f"record {index}: Satzart {satzart or '(missing)'} is not mapped; skipped")

    bundle: dict[str, Any] = {"resourceType": "Bundle", "type": "collection", "entry": entries}
    if provenance:
        targets = [{"reference": entry["fullUrl"]} for entry in entries]
        agents = []
        if sender_ref:
            agents.append({"type": {"text": "sender"}, "who": sender_ref})
        if receiver_ref:
            agents.append({"type": {"text": "receiver"}, "who": receiver_ref})
        bundle["entry"].append({
            "fullUrl": f"urn:uuid:{uuid.uuid4()}",
            "resource": {
                "resourceType": "Provenance",
                "target": targets,
                "recorded": date.today().isoformat(),
                "agent": agents or [{"who": {"display": "GDT interface"}}],
            },
        })
    return bundle, warnings


# --------------------------------------------------------------------------
# FHIR -> GDT
# --------------------------------------------------------------------------

def _first_identifier(identifiers: Sequence[dict[str, Any]], *, system: str | None = None) -> str:
    for identifier in identifiers:
        if system and identifier.get("system") != system:
            continue
        if identifier.get("value"):
            return str(identifier["value"])
    return ""


def fhir_to_gdt_spec(
    resources: dict[str, Any] | Sequence[dict[str, Any]],
    *,
    satzart: str | None = None,
    date_order: str = "ddmmyyyy",
    sender: str = "PVS-01",
    receiver: str = "GERAET-01",
    version: str = "03.50",
    insurance_system: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    from hl7_fhir_map import _index  # local import keeps the mapper private API contained

    grouped, _by_url = _index(resources)
    warnings: list[str] = []
    patient = (grouped.get("Patient") or [None])[0]
    if patient is None:
        raise Hl7Error("no Patient resource: a GDT record needs a patient context")

    report = (grouped.get("DiagnosticReport") or [None])[0]
    request = (grouped.get("ServiceRequest") or [None])[0]
    observations = grouped.get("Observation") or []

    if satzart is None:
        if report or observations:
            satzart = "6310"
        elif request:
            satzart = "6302"
        else:
            satzart = "6301"
    if satzart not in SATZART_NAMES:
        raise Hl7Error(f"unsupported Satzart {satzart!r}; expected 6301, 6302 or 6310")

    patient_identifiers = patient.get("identifier") or []
    patient_number = _first_identifier(patient_identifiers)
    if not patient_number:
        raise Hl7Error("Patient has no identifier; GDT field 3000 cannot be filled")

    fields: list[list[str]] = [["8000", satzart]]

    def add(fk: str, value: Any) -> None:
        text = "" if value is None else str(value)
        if text:
            fields.append([fk, text])

    add("8315", receiver)
    add("8316", sender)
    add("9218", version)
    add("3000", patient_number)
    _add_patient(fields, patient, add, date_order, insurance_system)

    if satzart == "6302":
        if request is None:
            warnings.append("6302 requested but the bundle has no ServiceRequest")
        else:
            code = _concept_code(request.get("code"))
            add("8402", code)
            add("6200", format_gdt_date(request.get("authoredOn") or request.get("occurrenceDateTime"), order=date_order))
            _add_notes(fields, add, request.get("note"))
    elif satzart == "6310":
        if report is None and not observations:
            warnings.append("6310 requested but the bundle has no DiagnosticReport or Observation")
        if report:
            add("8402", _concept_code(report.get("code")))
            add("6200", format_gdt_date(report.get("effectiveDateTime"), order=date_order))
            for line in str(report.get("conclusion") or "").splitlines():
                if line.strip():
                    add("6228", line.strip())
            _add_notes(fields, add, report.get("note"))
        elif observations:
            add("8402", _concept_code(observations[0].get("code")))
            add("6200", format_gdt_date(observations[0].get("effectiveDateTime"), order=date_order))
        for observation in observations:
            _add_notes(fields, add, observation.get("note"))
        for observation in observations:
            value = _observation_value(observation, warnings)
            if value:
                fields.append(["8420", value])
    elif satzart == "6301":
        pass

    spec = {"encoding": "latin-1", "records": [{"satzart": satzart, "fields": fields}]}
    return spec, warnings


def _add_patient(fields: list[list[str]], patient: dict[str, Any], add, date_order: str, insurance_system: str | None) -> None:
    names = patient.get("name") or [{}]
    official = next((item for item in names if item.get("use") == "official"), names[0])
    add("3101", official.get("family"))
    given = official.get("given") or []
    add("3102", given[0] if given else "")
    add("3103", format_gdt_date(patient.get("birthDate"), order=date_order))
    add("3110", {"male": "1", "female": "2"}.get(patient.get("gender") or "", ""))
    address = (patient.get("address") or [{}])[0]
    add("3106", address.get("city"))
    lines = address.get("line") or []
    add("3107", lines[0] if lines else "")
    for telecom in patient.get("telecom") or []:
        system = (telecom.get("system") or "").lower()
        if system == "phone" and telecom.get("value"):
            add("3618", telecom["value"])
        elif system == "email" and telecom.get("value"):
            add("3619", telecom["value"])
    if insurance_system:
        add("3105", _first_identifier(patient.get("identifier") or [], system=insurance_system))


def _add_notes(fields: list[list[str]], add, notes: Sequence[dict[str, Any]] | None) -> None:
    for note in notes or []:
        for line in str(note.get("text") or "").splitlines():
            if line.strip():
                add("6228", line.strip())


def _concept_code(concept: dict[str, Any] | None) -> str:
    if not concept:
        return ""
    coding = (concept.get("coding") or [{}])[0]
    return str(coding.get("code") or concept.get("text") or "")


def _number_text(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:.15g}"


def _observation_value(observation: dict[str, Any], warnings: list[str]) -> str:
    if "valueQuantity" in observation:
        quantity = observation.get("valueQuantity") or {}
        value = quantity.get("value")
        if value is None:
            return ""
        text = _number_text(value)
        if quantity.get("unit"):
            warnings.append(
                "quantity unit is not represented in GDT field 8420; the unit must be part of the "
                "device profile or a separate field (value kept without unit)"
            )
        return text
    if "valueString" in observation:
        return str(observation["valueString"])
    if "valueCodeableConcept" in observation:
        warnings.append(
            "coded observation value is not representable in GDT field 8420; value dropped "
            "(map it to a profile-defined code field instead)"
        )
        return ""
    if "valueInteger" in observation:
        return str(observation["valueInteger"])
    if "valueDecimal" in observation:
        return _number_text(observation["valueDecimal"])
    if "valueBoolean" in observation:
        return "1" if observation["valueBoolean"] else "0"
    if "valueDateTime" in observation:
        return str(observation["valueDateTime"])
    return ""


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _records(path: str, tool, encoding: str):
    records, issues, used_encoding = tool.read_file(Path(path), encoding)
    for issue in issues:
        if issue.severity in {"error", "warning"}:
            print(f"gdt {issue.severity}: {issue.message}", file=sys.stderr)
    return records, used_encoding


def cmd_gdt_to_fhir(args: argparse.Namespace) -> int:
    tool = load_gdt_tool(args.gdt_tool)
    records, _encoding = _records(args.file, tool, args.encoding)
    bundle, warnings = gdt_to_fhir_records(
        records,
        patient_system=args.patient_system,
        insurance_system=args.insurance_system,
        unit=args.unit,
        unit_code=args.unit_code,
        provenance=args.provenance,
    )
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    _write_json(args.out, bundle)
    return 0


def cmd_fhir_to_gdt(args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.file).read_text(encoding="utf-8-sig"))
    spec, warnings = fhir_to_gdt_spec(
        payload,
        satzart=args.satzart,
        date_order=args.date_order,
        sender=args.sender,
        receiver=args.receiver,
        version=args.version,
        insurance_system=args.insurance_system,
    )
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)

    if args.build:
        tool = load_gdt_tool(args.gdt_tool)
        pairs = tool.normalize_record(spec["records"][0])
        data, notes = tool.build_record(pairs, args.encoding, args.add_8100)
        tool.atomic_write(Path(args.build), data)
        for note in notes:
            print(f"note: {note}", file=sys.stderr)
        print(f"wrote {args.build} ({len(data)} bytes)")

    if args.out_json:
        _write_json(args.out_json, spec)
    elif not args.build:
        _write_json(None, spec)
    return 0


def cmd_gdt_to_hl7(args: argparse.Namespace) -> int:
    tool = load_gdt_tool(args.gdt_tool)
    records, _encoding = _records(args.file, tool, args.encoding)
    bundle, warnings = gdt_to_fhir_records(
        records,
        patient_system=args.patient_system,
        insurance_system=args.insurance_system,
        unit=args.unit,
        unit_code=args.unit_code,
    )
    satzart = records[0].satzart if records else "6301"
    message_type = HL7_FOR_SATZART.get(satzart)
    if message_type is None:
        raise Hl7Error(f"Satzart {satzart!r} has no HL7 message type mapping")
    message = fhir_to_hl7(
        bundle,
        message_type=message_type,
        sending_app=args.sending_app,
        sending_facility=args.sending_facility,
        receiving_app=args.receiving_app,
        receiving_facility=args.receiving_facility,
        version=args.version,
        encoding=args.hl7_encoding,
    )
    for warning in warnings + getattr(message, "warnings", []):
        print(f"warning: {warning}", file=sys.stderr)
    _write_text(args.out, message.to_er7(), encoding=args.hl7_encoding)
    return 0


def cmd_hl7_to_gdt(args: argparse.Namespace) -> int:
    message = Message.parse(Path(args.file).read_bytes(), encoding=args.hl7_encoding or None, strict=False)
    bundle, warnings = hl7_to_fhir_report(message, bundle_type="collection")
    code = message.message_type[0]
    satzart = args.satzart or SATZART_FOR_HL7.get(code)
    if satzart is None:
        raise Hl7Error(f"message type {code!r} has no GDT Satzart mapping")
    spec, mapping_warnings = fhir_to_gdt_spec(
        bundle,
        satzart=satzart,
        date_order=args.date_order,
        sender=args.sender,
        receiver=args.receiver,
        version=args.version,
        insurance_system=args.insurance_system,
    )
    for warning in warnings + mapping_warnings:
        print(f"warning: {warning}", file=sys.stderr)

    if args.build:
        tool = load_gdt_tool(args.gdt_tool)
        pairs = tool.normalize_record(spec["records"][0])
        data, notes = tool.build_record(pairs, args.encoding, args.add_8100)
        tool.atomic_write(Path(args.build), data)
        for note in notes:
            print(f"note: {note}", file=sys.stderr)
        print(f"wrote {args.build} ({len(data)} bytes)")
    if args.out_json:
        _write_json(args.out_json, spec)
    elif not args.build:
        _write_json(None, spec)
    return 0


def _write_json(path: str | None, payload: Any) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if path:
        Path(path).write_text(text + "\n", encoding="utf-8")
        print(f"wrote {path}")
    else:
        print(text)


def _write_text(path: str | None, text: str, *, encoding: str = "utf-8") -> None:
    if path:
        Path(path).write_text(text, encoding=encoding)
        print(f"wrote {path}")
    else:
        sys.stdout.write(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GDT <-> FHIR R4 <-> HL7 v2 bridge")
    sub = parser.add_subparsers(dest="command", required=True)

    to_fhir = sub.add_parser("gdt-to-fhir", help="map GDT records to a FHIR Bundle")
    to_fhir.add_argument("file")
    to_fhir.add_argument("--out", default=None)
    to_fhir.add_argument("--patient-system", default=None, help="Identifier.system for field 3000")
    to_fhir.add_argument("--insurance-system", default=None, help="Identifier.system for field 3105")
    to_fhir.add_argument("--unit", default=None, help="UCUM unit for field 8420, e.g. 'mm[Hg]'")
    to_fhir.add_argument("--unit-code", default=None)
    to_fhir.add_argument("--provenance", action="store_true")
    to_fhir.add_argument("--encoding", default="auto")
    to_fhir.add_argument("--gdt-tool", default=None)
    to_fhir.set_defaults(func=cmd_gdt_to_fhir)

    to_gdt = sub.add_parser("fhir-to-gdt", help="map FHIR resources to a GDT record")
    to_gdt.add_argument("file")
    to_gdt.add_argument("--satzart", default=None, choices=["6301", "6302", "6310"])
    to_gdt.add_argument("--out-json", default=None)
    to_gdt.add_argument("--build", default=None, metavar="OUT.gdt")
    to_gdt.add_argument("--add-8100", action="store_true")
    to_gdt.add_argument("--date-order", default="ddmmyyyy", choices=["ddmmyyyy", "yyyymmdd"])
    to_gdt.add_argument("--sender", default="PVS-01")
    to_gdt.add_argument("--receiver", default="GERAET-01")
    to_gdt.add_argument("--version", default="03.50")
    to_gdt.add_argument("--insurance-system", default=None)
    to_gdt.add_argument("--encoding", default="latin-1", choices=["latin-1", "utf-8", "cp1252", "cp437"])
    to_gdt.add_argument("--gdt-tool", default=None)
    to_gdt.set_defaults(func=cmd_fhir_to_gdt)

    to_hl7 = sub.add_parser("gdt-to-hl7", help="map a GDT file to an HL7 v2 message")
    to_hl7.add_argument("file")
    to_hl7.add_argument("--out", default=None)
    to_hl7.add_argument("--patient-system", default=None)
    to_hl7.add_argument("--insurance-system", default=None)
    to_hl7.add_argument("--unit", default=None)
    to_hl7.add_argument("--unit-code", default=None)
    to_hl7.add_argument("--sending-app", default="PVS")
    to_hl7.add_argument("--sending-facility", default="PRACTICE")
    to_hl7.add_argument("--receiving-app", default="KIS")
    to_hl7.add_argument("--receiving-facility", default="HOSPITAL")
    to_hl7.add_argument("--version", default="2.5.1")
    to_hl7.add_argument("--hl7-encoding", default="utf-8", choices=["utf-8", "latin-1"])
    to_hl7.add_argument("--encoding", default="auto")
    to_hl7.add_argument("--gdt-tool", default=None)
    to_hl7.set_defaults(func=cmd_gdt_to_hl7)

    to_gdt2 = sub.add_parser("hl7-to-gdt", help="map an HL7 v2 message to a GDT record")
    to_gdt2.add_argument("file")
    to_gdt2.add_argument("--satzart", default=None, choices=["6301", "6302", "6310"])
    to_gdt2.add_argument("--out-json", default=None)
    to_gdt2.add_argument("--build", default=None, metavar="OUT.gdt")
    to_gdt2.add_argument("--add-8100", action="store_true")
    to_gdt2.add_argument("--date-order", default="ddmmyyyy", choices=["ddmmyyyy", "yyyymmdd"])
    to_gdt2.add_argument("--sender", default="PVS-01")
    to_gdt2.add_argument("--receiver", default="GERAET-01")
    to_gdt2.add_argument("--version", default="03.50")
    to_gdt2.add_argument("--insurance-system", default=None)
    to_gdt2.add_argument("--encoding", default="latin-1", choices=["latin-1", "utf-8", "cp1252", "cp437"])
    to_gdt2.add_argument("--hl7-encoding", default=None)
    to_gdt2.add_argument("--gdt-tool", default=None)
    to_gdt2.set_defaults(func=cmd_hl7_to_gdt)

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
