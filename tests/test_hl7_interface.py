#!/usr/bin/env python3
"""Tests for the hl7-v2-interface skill. Run: python -m unittest discover -s tests -v"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
ASSETS = ROOT / "assets"
sys.path.insert(0, str(SCRIPTS))

import gdt_bridge  # noqa: E402
import hl7_fhir_map as mapper  # noqa: E402
from hl7core import (  # noqa: E402
    Hl7Error,
    Message,
    Separators,
    build_ack,
    escape_text,
    mllp_frame,
    mllp_unframe,
    unescape_text,
    validate,
)


def load(name: str) -> Message:
    return Message.parse((ASSETS / name).read_bytes(), strict=False)


def bundle_resources(bundle: dict) -> list[dict]:
    return [entry["resource"] for entry in bundle.get("entry", [])]


def find(bundle: dict, resource_type: str) -> list[dict]:
    return [item for item in bundle_resources(bundle) if item.get("resourceType") == resource_type]


class ParserTests(unittest.TestCase):
    def test_msh_indexing_and_values(self) -> None:
        message = load("adt_a01.hl7")
        self.assertEqual(message.message_type, ("ADT", "A01", "ADT_A01"))
        self.assertEqual(message.get("MSH-1"), "|")
        self.assertEqual(message.get("MSH-2"), "^~\\&")
        self.assertEqual(message.get("PID-5-1"), "DOE")
        self.assertEqual(message.get("PID-5-2"), "JANE")
        self.assertEqual(message.get("PID-7"), "19820411")
        self.assertEqual(message.get("PV1-19"), "V0012345")

    def test_roundtrip_is_stable(self) -> None:
        original = load("oru_r01.hl7")
        reparsed = Message.parse(original.to_er7(), strict=False)
        self.assertEqual(original.segments[0].fields, reparsed.segments[0].fields)
        self.assertEqual(
            [segment.name for segment in original.segments],
            [segment.name for segment in reparsed.segments],
        )
        for left, right in zip(original.segments, reparsed.segments):
            self.assertEqual(left.fields, right.fields)

    def test_trailing_empty_fields_are_trimmed(self) -> None:
        message = Message.parse("MSH|^~\\&|A|B|C|D|20260101||ADT^A01|1|P|2.5.1\rPID|1||||\r")
        self.assertTrue(message.to_er7().endswith("PID|1\r"))

    def test_escape_and_unescape(self) -> None:
        seps = Separators()
        text = "a|b^c~d\\e&f"
        encoded = escape_text(text, seps)
        self.assertNotIn("|", encoded)
        self.assertEqual(unescape_text(encoded, seps), text)

    def test_explicit_null_is_not_empty(self) -> None:
        message = Message.parse("MSH|^~\\&|A|B|C|D|20260101||ORU^R01|1|P|2.5.1\rOBX|1|ST|X||\"\"\r")
        self.assertTrue(message.segment("OBX").is_null(5))

    def test_batch_parsing(self) -> None:
        batch = (ASSETS / "adt_a01.hl7").read_text(encoding="utf-8") + "\r" + (ASSETS / "oru_r01.hl7").read_text(encoding="utf-8")
        messages = Message.parse_batch(batch)
        self.assertEqual([message.message_type[0] for message in messages], ["ADT", "ORU"])

    def test_ack_correlation(self) -> None:
        inbound = load("adt_a01.hl7")
        ack = build_ack(inbound, "AE", "Patient not found", error_code="101")
        self.assertEqual(ack.get("MSA-1"), "AE")
        self.assertEqual(ack.get("MSA-2"), inbound.get("MSH-10"))
        self.assertEqual(ack.get("MSH-3"), inbound.get("MSH-5"))
        self.assertEqual(ack.get("MSH-5"), inbound.get("MSH-3"))
        self.assertNotEqual(ack.get("MSH-10"), inbound.get("MSH-10"))
        self.assertIn("101", ack.get("ERR-3"))

    def test_validation_finds_missing_identifier(self) -> None:
        message = Message.parse("MSH|^~\\&|A|B|C|D|20260101||ADT^A01|1|P|2.5.1\rPID|1|||DOE^JANE\r")
        codes = {issue.code for issue in validate(message)}
        self.assertIn("PID-002", codes)
        self.assertIn("PV1-001", codes)

    def test_validation_accepts_samples(self) -> None:
        for name in ("adt_a01.hl7", "oru_r01.hl7", "orm_o01.hl7", "siu_s12.hl7", "mdm_t02.hl7"):
            errors = [issue for issue in validate(load(name)) if issue.severity == "error"]
            self.assertEqual(errors, [], name)

    def test_mllp_frame_roundtrip(self) -> None:
        payload = b"MSH|^~\\&|A|B|C|D|20260101||ACK|1|P|2.5.1\r"
        framed = mllp_frame(payload)
        self.assertTrue(framed.startswith(b"\x0b"))
        self.assertTrue(framed.endswith(b"\x1c\r"))
        extracted, rest = mllp_unframe(framed + b"next")
        self.assertEqual(extracted, payload)
        self.assertEqual(rest, b"next")


class MapperTests(unittest.TestCase):
    def test_ts_conversion(self) -> None:
        self.assertEqual(mapper.ts_to_iso("19820411", date_only=True), "1982-04-11")
        self.assertEqual(mapper.ts_to_iso("20260314153000"), "2026-03-14T15:30:00")
        self.assertEqual(mapper.iso_to_ts("2026-03-14T15:30:00+01:00"), "20260314153000+0100")
        self.assertEqual(mapper.iso_to_ts("2026-03-14"), "20260314")
        self.assertEqual(mapper.iso_to_ts("not-a-date"), "")

    def test_patient_mapping(self) -> None:
        bundle, _warnings = mapper.hl7_to_fhir_report(load("oru_r01.hl7"))
        patient = find(bundle, "Patient")[0]
        identifier = patient["identifier"][0]
        self.assertEqual(identifier["value"], "123456")
        self.assertEqual(identifier["system"], "urn:oid:1.2.3.4")
        self.assertEqual(patient["name"][0]["family"], "DOE")
        self.assertEqual(patient["birthDate"], "1982-04-11")
        self.assertEqual(patient["gender"], "female")

    def test_observation_and_report_mapping(self) -> None:
        bundle, _warnings = mapper.hl7_to_fhir_report(load("oru_r01.hl7"))
        report = find(bundle, "DiagnosticReport")[0]
        observations = find(bundle, "Observation")
        self.assertEqual(report["status"], "final")
        self.assertEqual(report["code"]["coding"][0]["code"], "CBC")
        self.assertEqual(len(observations), 2)

        hemoglobin = observations[0]
        self.assertEqual(hemoglobin["code"]["coding"][0]["system"], mapper.LOINC)
        self.assertEqual(hemoglobin["valueQuantity"]["value"], 13.7)
        self.assertEqual(hemoglobin["valueQuantity"]["code"], "g/dL")
        self.assertEqual(hemoglobin["valueQuantity"]["system"], mapper.UCUM)
        self.assertEqual(hemoglobin["interpretation"][0]["coding"][0]["code"], "N")
        self.assertEqual(hemoglobin["referenceRange"][0]["text"], "12.0-16.0")
        self.assertEqual(hemoglobin["note"][0]["text"], "Slightly below previous value; verify sample.")

        coded = observations[1]
        self.assertEqual(coded["valueCodeableConcept"]["coding"][0]["code"], "260373001")
        self.assertEqual(coded["valueCodeableConcept"]["coding"][0]["system"], mapper.SNOMED)

    def test_references_resolve_and_urls_are_unique(self) -> None:
        bundle, _warnings = mapper.hl7_to_fhir_report(load("oru_r01.hl7"))
        urls = [entry["fullUrl"] for entry in bundle["entry"]]
        self.assertEqual(len(urls), len(set(urls)))
        known = set(urls)
        report = find(bundle, "DiagnosticReport")[0]
        for reference in report.get("result", []) + report.get("basedOn", []) + report.get("performer", []):
            self.assertIn(reference["reference"], known)

    def test_deterministic_urls(self) -> None:
        first, _ = mapper.hl7_to_fhir_report(load("adt_a01.hl7"))
        second, _ = mapper.hl7_to_fhir_report(load("adt_a01.hl7"))
        self.assertEqual([entry["fullUrl"] for entry in first["entry"]], [entry["fullUrl"] for entry in second["entry"]])

    def test_transaction_bundle_has_requests(self) -> None:
        bundle, _warnings = mapper.hl7_to_fhir_report(load("adt_a01.hl7"), bundle_type="transaction")
        for entry in bundle["entry"]:
            self.assertIn("request", entry)

    def test_fhir_to_hl7_roundtrip(self) -> None:
        bundle, _warnings = mapper.hl7_to_fhir_report(load("oru_r01.hl7"))
        message = mapper.fhir_to_hl7(bundle, message_type="ORU^R01", control_id="TEST1", version="2.5.1")
        self.assertEqual(message.message_type, ("ORU", "R01", "ORU_R01"))
        self.assertEqual(message.get("PID-3-1"), "123456")
        self.assertEqual(message.get("PID-7"), "19820411")
        self.assertEqual(message.get("OBR-4-1"), "CBC")
        self.assertEqual(message.get("OBX-5"), "13.7")
        self.assertEqual(message.get("OBX-6-1"), "g/dL")
        self.assertEqual(message.get("OBX-11"), "F")

    def test_narrative_line_breaks_are_escaped(self) -> None:
        bundle, _warnings = mapper.hl7_to_fhir_report(load("oru_r01.hl7"))
        for observation in find(bundle, "Observation"):
            for note in observation.get("note") or []:
                note["text"] = "line1\nline2"
        message = mapper.fhir_to_hl7(bundle, message_type="ORU^R01")
        notes = [segment for segment in message.segments if segment.name == "NTE"]
        self.assertTrue(notes)
        self.assertIn("\\.br\\", notes[0].get(3))
        self.assertNotIn("\n", message.to_er7())

    def test_orm_order_mapping(self) -> None:
        bundle, _warnings = mapper.hl7_to_fhir_report(load("orm_o01.hl7"))
        request = find(bundle, "ServiceRequest")[0]
        self.assertEqual(request["status"], "active")
        self.assertEqual(request["code"]["coding"][0]["code"], "CBC")
        self.assertEqual(request["priority"], "routine")
        self.assertEqual(request["note"][0]["text"], "Routine admission panel.")


class SchedulingDocumentTests(unittest.TestCase):
    def test_siu_mapping(self) -> None:
        bundle, warnings = mapper.hl7_to_fhir_report(load("siu_s12.hl7"))
        appointment = find(bundle, "Appointment")[0]
        self.assertEqual(appointment["status"], "booked")
        self.assertEqual(appointment["start"], "2026-04-01T10:00:00")
        self.assertEqual(appointment["end"], "2026-04-01T10:30:00")
        self.assertEqual(appointment["minutesDuration"], 30)
        self.assertEqual(appointment["serviceType"][0]["coding"][0]["code"], "CARD-ECHO")
        self.assertEqual(appointment["comment"], "Patient asked for a morning appointment.")
        roles = {item["type"][0]["coding"][0]["code"] for item in appointment["participant"]}
        self.assertEqual(roles, {"SBJ", "ATND", "LOC", "PPRF"})
        self.assertFalse([warning for warning in warnings if "SCH-25" in warning])

        identifiers = {}
        for identifier in appointment["identifier"]:
            coding = ((identifier.get("type") or {}).get("coding") or [{}])[0]
            identifiers[coding.get("code") or "GROUP"] = identifier["value"]
        self.assertEqual(identifiers["PLAC"], "APT-1001")
        self.assertEqual(identifiers["FILL"], "APT-1001")
        self.assertEqual(identifiers["GROUP"], "GRP-1")

    def test_siu_roundtrip(self) -> None:
        bundle, _warnings = mapper.hl7_to_fhir_report(load("siu_s12.hl7"))
        message = mapper.fhir_to_hl7(bundle, control_id="SIU1")
        self.assertEqual(message.message_type, ("SIU", "S12", "SIU_S12"))
        self.assertEqual(message.get("SCH-1-1"), "APT-1001")
        self.assertEqual(message.get("SCH-4-1"), "GRP-1")
        self.assertEqual(message.get("SCH-6-1"), "EVT")
        self.assertEqual(message.get("SCH-7-1"), "ROUTINE")
        self.assertEqual(message.get("AIS-3-1"), "CARD-ECHO")
        self.assertEqual(message.get("AIP-3-1"), "9001")
        self.assertEqual(message.get("AIP-4-1"), "ATND")
        self.assertEqual(message.get("AIL-3-1"), "CARD")
        self.assertEqual(message.get("AIG-3-1"), "US-01")
        self.assertEqual(message.get("NTE-3"), "Patient asked for a morning appointment.")

    def test_siu_status_triggers(self) -> None:
        expected = {"S15": "cancelled", "S17": "entered-in-error", "S26": "noshow"}
        for trigger, status in expected.items():
            message = load("siu_s12.hl7")
            message.set("MSH-9", f"SIU^{trigger}^SIU_{trigger}")
            message.set("SCH-25", "")
            bundle, _warnings = mapper.hl7_to_fhir_report(message)
            self.assertEqual(find(bundle, "Appointment")[0]["status"], status, trigger)

    def test_mdm_mapping(self) -> None:
        bundle, warnings = mapper.hl7_to_fhir_report(load("mdm_t02.hl7"))
        document = find(bundle, "DocumentReference")[0]
        self.assertEqual(document["status"], "current")
        self.assertEqual(document["docStatus"]["coding"][0]["code"], "final")
        self.assertEqual(document["docStatus"]["coding"][0]["system"], "http://hl7.org/fhir/document-reference-status")
        self.assertIn("AU", document["docStatus"]["text"])
        self.assertEqual(document["type"]["coding"][0]["code"], "PN")
        self.assertEqual(document["description"], "Signed by attending physician.")
        self.assertEqual(document["securityLabel"][0]["coding"][0]["system"],
                         "http://terminology.hl7.org/CodeSystem/v3-Confidentiality")
        attachment = document["content"][0]["attachment"]
        self.assertEqual(attachment["contentType"], "text/plain")
        self.assertEqual(attachment["data"], "UHJvZ3Jlc3Mgbm90ZSBjb250ZW50")
        self.assertEqual(attachment["size"], 21)
        urls = {entry["fullUrl"] for entry in bundle["entry"]}
        self.assertIn(document["context"]["encounter"][0]["reference"], urls)
        self.assertTrue(document["author"])
        self.assertTrue(document["authenticator"])
        self.assertFalse([warning for warning in warnings if "not usable" in warning])

    def test_mdm_roundtrip(self) -> None:
        bundle, _warnings = mapper.hl7_to_fhir_report(load("mdm_t02.hl7"))
        message = mapper.fhir_to_hl7(bundle, control_id="MDM1")
        self.assertEqual(message.message_type, ("MDM", "T02", "MDM_T02"))
        self.assertEqual(message.get("TXA-12-1"), "DOC-1001")
        self.assertEqual(message.get("TXA-14-1"), "ORD-5001")
        self.assertEqual(message.get("TXA-15-1"), "FIL-5001")
        self.assertEqual(message.get("TXA-17"), "AU")
        self.assertEqual(message.get("OBX-2"), "ED")
        self.assertEqual(message.get("OBX-5-3"), "TEXT")
        self.assertEqual(message.get("OBX-5-4"), "Base64")
        self.assertEqual(message.get("OBX-5-5"), "UHJvZ3Jlc3Mgbm90ZSBjb250ZW50")
        self.assertEqual(message.get("NTE-3"), "Signed by attending physician.")

    def test_mdm_cancel_and_addendum(self) -> None:
        cancelled = load("mdm_t02.hl7")
        cancelled.set("MSH-9", "MDM^T11^MDM_T11")
        bundle, _warnings = mapper.hl7_to_fhir_report(cancelled)
        self.assertEqual(find(bundle, "DocumentReference")[0]["status"], "entered-in-error")

        addendum = load("mdm_t02.hl7")
        addendum.set("MSH-9", "MDM^T06^MDM_T06")
        addendum.set("TXA-13", "DOC-0900^HOSP")
        bundle, _warnings = mapper.hl7_to_fhir_report(addendum)
        document = find(bundle, "DocumentReference")[0]
        self.assertEqual(document["relatesTo"][0]["code"], "appends")
        self.assertEqual(document["relatesTo"][0]["target"]["identifier"]["value"], "DOC-0900")
        message = mapper.fhir_to_hl7(bundle, control_id="MDM2")
        self.assertEqual(message.message_type[1], "T06")
        self.assertEqual(message.get("TXA-13-1"), "DOC-0900")


class GdtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.tool = gdt_bridge.load_gdt_tool()
        except Hl7Error as error:  # pragma: no cover - environment dependent
            raise unittest.SkipTest(str(error))

    def records(self, name: str):
        records, _issues, _encoding = self.tool.read_file(ASSETS / name)
        return records

    def test_gdt_order_to_fhir(self) -> None:
        bundle, warnings = gdt_bridge.gdt_to_fhir_records(self.records("sample-6302.gdt"))
        patient = find(bundle, "Patient")[0]
        self.assertEqual(patient["identifier"][0]["value"], "P-1042")
        self.assertEqual(patient["name"][0]["family"], "Müller")
        self.assertEqual(patient["birthDate"], "1978-03-14")
        request = find(bundle, "ServiceRequest")[0]
        self.assertEqual(request["code"]["coding"][0]["code"], "1234")
        self.assertEqual(warnings, [])

    def test_gdt_result_to_fhir(self) -> None:
        bundle, warnings = gdt_bridge.gdt_to_fhir_records(self.records("sample-6310.gdt"))
        report = find(bundle, "DiagnosticReport")[0]
        self.assertEqual(report["status"], "final")
        self.assertIn("Unauffälliger Befund", report["conclusion"])
        observations = find(bundle, "Observation")
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["valueString"], "128")
        self.assertTrue(any("no unit" in warning for warning in warnings))

    def test_gdt_result_with_unit(self) -> None:
        bundle, _warnings = gdt_bridge.gdt_to_fhir_records(
            self.records("sample-6310.gdt"), unit="1", unit_code="1"
        )
        observation = find(bundle, "Observation")[0]
        self.assertEqual(observation["valueQuantity"], {"value": 128.0, "unit": "1", "code": "1"})

    def test_gdt_to_hl7(self) -> None:
        bundle, _warnings = gdt_bridge.gdt_to_fhir_records(self.records("sample-6302.gdt"))
        message = mapper.fhir_to_hl7(bundle, message_type="ORM^O01", control_id="GDT1")
        self.assertEqual(message.get("PID-3-1"), "P-1042")
        self.assertEqual(message.get("PID-5-1"), "Müller")
        self.assertEqual(message.get("OBR-4-1"), "1234")

    def test_hl7_to_gdt_spec(self) -> None:
        message = load("oru_r01.hl7")
        bundle, _warnings = mapper.hl7_to_fhir_report(message)
        spec, warnings = gdt_bridge.fhir_to_gdt_spec(bundle, satzart="6310", date_order="ddmmyyyy")
        fields = dict(spec["records"][0]["fields"])
        self.assertEqual(fields["8000"], "6310")
        self.assertEqual(fields["3000"], "123456")
        self.assertEqual(fields["3101"], "DOE")
        self.assertEqual(fields["3103"], "11041982")
        self.assertEqual(fields["3110"], "2")
        self.assertEqual(fields["8402"], "CBC")
        self.assertEqual(fields["8420"], "13.7")
        self.assertTrue(any("coded observation" in warning for warning in warnings))

    def test_gdt_build_roundtrip(self) -> None:
        spec, _warnings = gdt_bridge.fhir_to_gdt_spec(
            mapper.hl7_to_fhir(load("oru_r01.hl7")), satzart="6310"
        )
        pairs = self.tool.normalize_record(spec["records"][0])
        data, _notes = self.tool.build_record(pairs, "latin-1", False)
        records, issues, _encoding = self.tool.parse_bytes(data)
        self.assertFalse([issue for issue in issues if issue.severity == "error"])
        self.assertEqual(records[0].satzart, "6310")
        self.assertEqual(records[0].get("3000"), "123456")


if __name__ == "__main__":
    unittest.main()
