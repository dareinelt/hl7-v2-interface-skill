# Production checklist

## Data protection

- Treat raw HL7, GDT and FHIR payloads as PHI/personal health data.
- Encrypt at rest (archive, DLQ, database) and in transit (MLLPS, HTTPS).
- Restrict archive and DLQ access to the integration service account and a small
  on-call group.
- Define retention per store: inbox, archive, DLQ, logs, backups.
- Redact identifiers from error messages, stack traces and metrics labels.
- Never copy production messages into tickets, chat, test fixtures or source control.

## Logging and audit

Log per message:

- Timestamp, direction, interface/channel name
- Message type (`ADT^A01`), control ID, version, byte count
- Outcome (`AA`/`AE`/`AR`, stored/duplicate/rejected)
- Latency and retry count
- Archive/DLQ path, not the content

Do not log:

- Patient names, identifiers, addresses, phone numbers
- OBX values or free-text findings
- Full raw messages outside the encrypted archive

Emit an audit event per message (sender, receiver, type, outcome). For IHE
deployments, align with ATNA. Keep audit and clinical logs separate.

## Idempotency and state

- Persist the idempotency key (`MSH-3` + `MSH-4` + `MSH-10`) with a payload hash.
- Store the outcome so a retry can return the same ACK without reprocessing.
- Make business updates idempotent (upsert by patient + encounter/order).
- Appointments: key by SCH-1/SCH-2 in addition to MSH-10; a cancellation (S15) must
  keep the history, never delete the row.
- Documents: version by TXA-12 (unique document number) and link addenda/replacements
  through TXA-13; never overwrite the previous binary content.
- Handle out-of-order ADT (A08 before A01): apply events in receipt order unless the
  contract defines sequencing.

## Monitoring

Expose at minimum:

- Last successful inbound and outbound message timestamp per interface
- Messages per minute, by type and outcome
- Error rate by class (parse, validation, business, transport)
- ACK latency percentiles and timeout count
- Queue depth and age of the oldest unprocessed message
- Duplicate and conflict counts
- DLQ size and retention age
- TLS certificate expiry

Alert on a silent interface: no messages for longer than the expected interval is
usually an outage, not calm.

## Resilience

- Bounded retries with exponential backoff and jitter.
- Circuit breaker for a failing downstream, with a clear resume procedure.
- Graceful shutdown: stop accepting, drain in-flight handlers, close sockets.
- Backpressure: bounded worker pool and queue; reject with `AE` instead of OOM.
- Replay tool: re-inject archived messages by control ID with an audit trail.

## Testing

- Unit tests for parsing, escaping, explicit nulls and ACK correlation.
- Golden-file corpus of real vendor quirks (anonymised or synthetic).
- Negative tests: truncated frame, missing MSH-10, wrong version, duplicate MSH-10,
  unknown message type, oversized message.
- Round-trip tests: parse -> serialize -> parse equality; HL7 -> FHIR -> HL7 for the
  fields you own.
- Conformance tests with `hl7apy` or the engine's validator when a profile is part of
  the contract.
- Load test at peak messages per second with realistic message sizes.
- Failover test: kill the receiver mid-stream and verify no message is acknowledged
  but lost.

## Go-live

- Written interface contract: version, encoding, delimiters, message types, trigger
  events, identifier systems, code systems, ACK semantics, retry policy, contact
  escalation.
- Test and production separated by configuration, with a visible test marker
  (MSH-11 `T`).
- Certificate and trust store installed and monitored.
- Runbook for DLQ, replay, duplicate conflict and certificate rotation.
- Sign-off from the clinical/integration owner, not only from IT.
- Post-go-live observation window with message counts compared against the sender's.

## Regulatory notes (Germany/EU)

- GDT is maintained by QMS e.V.; use the negotiated profile, not a generic table.
- ISiK profiles apply to FHIR interfaces in hospitals; validate resources against the
  target profile.
- IHE profiles and gematik specifications apply to cross-organisation exchange.
- A medical device that transmits results may be subject to MDR/ASTM/IVDR constraints;
  clarify the intended use with the manufacturer before changing the interface.
