# MLLP and acknowledgements

## Framing

MLLP wraps one HL7 message per frame:

```text
0x0B  <HL7 message bytes>  0x1C 0x0D
VT    payload              FS   CR
```

- Read until `0x1C`, not until EOF and not until newline. Senders keep the socket
  open and may send the next message on the same connection.
- Accept a missing leading VT on inbound frames from sloppy senders, but always send
  a leading VT.
- Strip the payload before parsing; do not include VT/FS in the message.
- Enforce a maximum message size and a read timeout.

MLLPS is MLLP inside TLS, typically on port 2575 or a site-specific port. Mutual TLS
is common in IHE ATNA deployments.

## ACK choreography

| Inbound MSH-15/16 | Expected response |
|-------------------|-------------------|
| empty / empty | Application ACK: `AA`, `AE` or `AR` |
| `AL` (always) | Application ACK required |
| `NE` (never) | No ACK |
| `ER`/`SU` with commit ack requested | Commit ACK: `CA`, `CE`, `CR` |

Do not send `CA`/`CE`/`CR` unless the sender requested a commit acknowledgement.
Receivers that do not implement commit semantics answer `AA` and the sender treats it
as an application ack.

### ACK codes

| Code | Meaning | Sender action |
|------|---------|---------------|
| AA | Application accept | Proceed; message is committed |
| AE | Application error | Correct the data and retry, or surface to a user |
| AR | Application reject | Do not retry unchanged; fix syntax/version/segment |
| CA | Commit accept | Message stored durably |
| CE | Commit error | Retry after storage is available |
| CR | Commit reject | Do not retry unchanged |

- `MSA-2` must echo the original `MSH-10`. Treat a missing or mismatched MSA-2 as a
  protocol error even when MSA-1 is `AA`.
- `ERR-3` carries the error code and text (HL7 table 0357). Common values: 100
  segment sequence, 101 required field missing, 102 data type error, 103 table value
  not found, 200 unsupported message type, 207 application internal error.
- Never put patient identifiers, names or free-text values into `ERR-3` or logs.

## Ordering and durability

1. Parse and persist the raw message or start a transaction.
2. Run business processing.
3. Commit.
4. Send the ACK.

A positive ACK before step 3 makes message loss invisible. If the business operation
is asynchronous, either store the message durably before ACK, or use the commit ACK
semantics explicitly.

## Idempotency

- Key: `MSH-10` plus sending application/facility (MSH-3/4), because senders reuse
  control IDs after a restart.
- Store the key with a payload hash and outcome.
- Same key + same hash = duplicate; acknowledge again without reprocessing.
- Same key + different hash = conflict; quarantine and alert.
- Do not use PID-3 or a visit number as the message key.

## Retries

Classify failures:

| Class | Example | Action |
|-------|---------|--------|
| Transient | Connection reset, timeout, storage unavailable | Retry with exponential backoff |
| Protocol | No MLLP frame, unparseable MSH, ACK without MSA-2 | Do not retry blindly; investigate |
| Business | Unknown patient, order cancelled | `AE`, surface to the user |
| Permanent | Unsupported version or message type | `AR`, no retry |

Bound the attempts and move exhausted messages to a dead-letter store. A DLQ holding
raw HL7 is a PHI store: encrypt it, restrict access and define a retention period.

## TLS and IHE ATNA

- Use `MLLPS` (TLS 1.2+) with a server certificate trusted by both sides.
- For mutual TLS, require a client certificate and map it to the sending application.
- IHE ATNA ties transport security to audit logging. Emit an audit event per message
  with sender, receiver, message type, control ID and outcome - never with content.
- Verify hostnames; do not ship `--insecure`/`CERT_NONE` to production.

## Interface engines

Engines terminate MLLP, apply routing and transformation, and answer ACKs on behalf
of downstream systems. Know which component owns the ACK:

- If the engine answers `AA` before the downstream commit, the engine owns durability.
- If the engine forwards the ACK from downstream, the channel timeout must cover the
  downstream latency.

Mirth Connect/NextGen Connect has been proprietary since 4.6 (March 2025). Open
forks derived from 4.5.2 are Open Integration Engine and BridgeLink. Commercial
alternatives: Rhapsody, Iguana, Qvera, InterSystems Health Connect, Corepoint.

## Minimal listener checklist

- Bind to the agreed interface, not `0.0.0.0`, unless the network design requires it.
- One handler per connection, with a worker pool and backpressure.
- Size limit, read timeout, write timeout, graceful shutdown.
- Archive the exact received bytes before parsing.
- Answer `AE` with an ERR segment when parsing fails; never drop silently.
- Log type, control ID, byte count, outcome and archive path.
- Health endpoint or metric that exposes last successful message and queue age.
