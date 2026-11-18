# Python and integration ecosystem

## The bundled stdlib approach

`hl7core.py` needs no dependencies. It is suited to:

- Deterministic parsing and serialization with exact field preservation.
- MLLP client/server with TLS.
- Structural validation and ACK generation.
- A conservative HL7 <-> FHIR mapping that reports gaps.

It is not a conformance-profile validator, does not ship full HL7 dictionaries and
does not implement every segment. Add a library when you need those.

## Python libraries

| Library | Strength | Limitation |
|---------|----------|------------|
| `hl7apy` | Specification-aware object model, validates against v2.1-v2.8.2 structures, semantic names | Heavier; slower on large streams |
| `python-hl7` | Small container/accessor model, MLLP client/server helpers | No profile validation |
| `hl7` (parser) | Minimal, fast, indexable access | Manual structural checks |

Install only when needed:

```bash
python -m pip install hl7apy
python -m pip install python-hl7
```

Recommended split:

- Use `hl7core` at the wire boundary: exact bytes, MLLP, ACK.
- Use `hl7apy` when a conformance profile or strict version validation is part of the
  contract, or when you need generated message classes.
- Use `python-hl7` for a quick second opinion in tests.

Do not parse the same message with two libraries in production just to compare; pick
one and keep the other for the test oracle.

## Other stacks

| Stack | Library |
|-------|---------|
| Java | HAPI HL7v2 (`PipeParser`, `Terser`, `SimpleServer`, conformance profiles) |
| .NET | NHapi (strongly typed port of HAPI), or the vendor SDK |
| Node/TypeScript | `@cosyte/hl7`, `@cosyte/mllp` (MIT), or `simple-hl7` |
| Ballerina | `health.hl7v23` ... `health.hl7v28`, `health.clients.hl7` |
| Rust | `hl7` crates / `hl7-rust` workspace |

The mapper output is plain JSON, so a Java or .NET service can consume the FHIR
Bundle without sharing Python code.

## Interface engines

| Engine | Note |
|--------|------|
| Mirth Connect / NextGen Connect | Proprietary since 4.6 (March 2025); 4.5.2 is the last MPL release |
| Open Integration Engine | Fork of Mirth 4.5.2 |
| BridgeLink | Fork of Mirth 4.5.2, Java 17+ |
| Rhapsody | Commercial, strong KLAS record |
| Iguana / IguanaX | Commercial, lightweight |
| Qvera QIE | Commercial |
| InterSystems Health Connect | Enterprise |
| Apache Camel HL7 | Library-level routing, no channel GUI |

Use an engine when you need visual channel design, many interfaces and operational
tooling. Use application code when the interface is part of your product and must
share the domain model.

## Cloud services

| Service | HL7 v2 support |
|---------|----------------|
| Azure Logic Apps | HL7 connector (decode/encode), MLLP via hybrid adapter |
| Azure Health Data Services | FHIR service, conversion via connectors |
| Google Cloud Healthcare API | HL7v2 store, REST API, open-source MLLP adapter |
| AWS HealthLake | FHIR store, import jobs; HL7 v2 needs your own adapter |

Cloud stores are not a substitute for an MLLP endpoint that terminates and
acknowledges a feed. Check who answers the ACK and who owns durability.

## Testing tools

- `hl7_tool.py serve` / `send`: a minimal MLLP pair for integration tests.
- HAPI TestPanel: desktop HL7 viewer and MLLP simulator.
- `hl7apy` validation in CI for a conformance-profile corpus.
- 7-Zip/xxd for inspecting MLLP captures (`0x0B`, `0x1C`, `0x0D`).
- WireMock or a small socket server for failure injection (timeouts, partial frames,
  duplicate ACKs).
