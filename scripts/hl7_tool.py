#!/usr/bin/env python3
"""Command line tool for HL7 v2.x: parse, validate, acknowledge, MLLP client/server.

Examples:
  python hl7_tool.py parse assets/adt_a01.hl7 --tree
  python hl7_tool.py validate assets/oru_r01.hl7 --strict
  python hl7_tool.py ack assets/adt_a01.hl7 --code AE --text "Patient unknown"
  python hl7_tool.py send 127.0.0.1 6661 --file assets/adt_a01.hl7
  python hl7_tool.py serve --port 6661 --out inbox
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import socketserver
import ssl
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

try:
    from hl7core import (
        Hl7Error,
        Message,
        build_ack,
        build_minimal_ack,
        mllp_frame,
        mllp_unframe,
        send_mllp,
        strip_mllp,
        validate,
    )
except ImportError:  # pragma: no cover - executed as a module
    from .hl7core import (  # type: ignore
        Hl7Error,
        Message,
        build_ack,
        build_minimal_ack,
        mllp_frame,
        mllp_unframe,
        send_mllp,
        strip_mllp,
        validate,
    )

MAX_INBOUND_BYTES = 16 * 1024 * 1024


def read_message(path: str, encoding: str | None, *, strict: bool = True) -> Message:
    data = Path(path).read_bytes()
    return Message.parse(data, encoding=encoding or None, strict=strict)


def _print_issues(issues, stream=sys.stdout) -> None:
    for issue in issues:
        print(str(issue), file=stream)


def command_parse(args: argparse.Namespace) -> int:
    message = read_message(args.file, args.encoding, strict=not args.lenient)
    if args.json:
        print(json.dumps(message.to_dict(), ensure_ascii=False, indent=2))
        return 0

    code, trigger, structure = message.message_type
    print(f"type={code}^{trigger} structure={structure or '-'} version={message.version or '-'}")
    print(f"control_id={message.control_id or '-'} encoding={message.encoding} segments={len(message.segments)}")
    print(f"separators: field={message.separators.field!r} component={message.separators.component!r} "
          f"repetition={message.separators.repetition!r} escape={message.separators.escape!r} "
          f"subcomponent={message.separators.subcomponent!r}")
    if args.tree or args.show_values:
        for segment in message.segments:
            if args.show_values:
                rendered = "|".join([segment.name] + segment.fields)
                print(f"  {rendered[:args.width]}")
            else:
                print(f"  {segment.name:<4} fields={len(segment.fields)}")
    return 0


def command_validate(args: argparse.Namespace) -> int:
    try:
        message = read_message(args.file, args.encoding, strict=False)
    except Hl7Error as error:
        if args.json:
            print(json.dumps({"file": args.file, "valid": False, "issues": [
                {"severity": "error", "code": "PARSE", "message": str(error)}
            ]}, ensure_ascii=False, indent=2))
        else:
            print(f"{args.file}: error: {error}")
        return 1

    issues = validate(message, strict=args.strict)
    errors = [item for item in issues if item.severity == "error"]
    warnings = [item for item in issues if item.severity == "warning"]

    if args.json:
        print(json.dumps({
            "file": args.file,
            "valid": not errors,
            "errors": len(errors),
            "warnings": len(warnings),
            "issues": [item.as_dict() for item in issues],
        }, ensure_ascii=False, indent=2))
    else:
        print(f"{args.file}: {len(message.segments)} segments, {len(errors)} errors, {len(warnings)} warnings")
        _print_issues(issues)
    return 1 if errors else 0


def command_ack(args: argparse.Namespace) -> int:
    message = read_message(args.file, args.encoding, strict=False)
    ack = build_ack(message, code=args.code, text=args.text or "", echo_trigger=args.echo_trigger)
    payload = ack.to_er7()
    if args.out:
        atomic_write(Path(args.out), payload.encode(ack.encoding))
        print(f"wrote {args.out} ({len(payload)} chars)")
    else:
        sys.stdout.write(payload)
    return 0


def command_send(args: argparse.Namespace) -> int:
    message = read_message(args.file, args.encoding, strict=False)
    ack = send_mllp(
        message,
        args.host,
        args.port,
        timeout=args.timeout,
        encoding=args.encoding or None,
        tls=args.tls,
        ca_file=args.ca_file,
        cert_file=args.cert_file,
        key_file=args.key_file,
        insecure=args.insecure,
    )
    code = ack.get_text("MSA-1") or "?"
    expected = ack.get_text("MSA-2")
    correlated = not expected or expected == message.control_id
    ok = code in {"AA", "CA"} and correlated
    print(f"ACK code={code} message_control_id={message.control_id!r} echoed={expected!r}")
    if not correlated:
        print(
            f"protocol error: MSA-2 {expected!r} does not match MSH-10 {message.control_id!r}",
            file=sys.stderr,
        )
    if args.ack_out:
        atomic_write(Path(args.ack_out), ack.to_bytes())
    if args.json:
        print(json.dumps(ack.to_dict(), ensure_ascii=False, indent=2))
    if not ok:
        for segment in ack.segments_named("ERR"):
            print(f"ERR: {segment.get_text(3)}")
        return 1
    return 0


def command_frame(args: argparse.Namespace) -> int:
    payload = strip_mllp(Path(args.file).read_bytes())
    framed = mllp_frame(payload)
    if args.out:
        atomic_write(Path(args.out), framed)
        print(f"wrote {args.out} ({len(framed)} bytes)")
    else:
        sys.stdout.buffer.write(framed)
    return 0


def command_unframe(args: argparse.Namespace) -> int:
    data = Path(args.file).read_bytes()
    payload, rest = mllp_unframe(data)
    if payload is None:
        print("no MLLP end block found", file=sys.stderr)
        return 1
    if rest.strip():
        print(f"warning: {len(rest)} trailing bytes after the first frame", file=sys.stderr)
    if args.out:
        atomic_write(Path(args.out), payload)
        print(f"wrote {args.out} ({len(payload)} bytes)")
    else:
        sys.stdout.buffer.write(payload)
    return 0


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, handler_class, *, ssl_context=None):
        self.ssl_context = ssl_context
        super().__init__(server_address, handler_class)

    def get_request(self):
        sock, address = super().get_request()
        if self.ssl_context is not None:
            sock = self.ssl_context.wrap_socket(sock, server_side=True)
        return sock, address


class MllpHandler(socketserver.BaseRequestHandler):
    server: _Server

    def handle(self) -> None:
        options = self.server.options
        self.request.settimeout(options.timeout)
        buffer = b""
        try:
            while True:
                while b"\x1c" not in buffer:
                    chunk = self.request.recv(65536)
                    if not chunk:
                        return
                    buffer += chunk
                    if len(buffer) > MAX_INBOUND_BYTES:
                        raise Hl7Error("inbound message exceeds size limit")
                payload, buffer = mllp_unframe(buffer)
                if payload is None:
                    continue
                self.process(payload, options)
        except (socket.timeout, ConnectionError) as error:
            print(f"connection error from {self.client_address[0]}: {error}", file=sys.stderr)
        except Hl7Error as error:
            print(f"protocol error from {self.client_address[0]}: {error}", file=sys.stderr)
        except ssl.SSLError as error:
            print(f"TLS error from {self.client_address[0]}: {error}", file=sys.stderr)

    def process(self, payload: bytes, options) -> None:
        try:
            message = Message.parse(payload, strict=False)
            issues = validate(message, strict=options.reject_warnings)
            blocking = [item for item in issues if item.severity == "error"]
            if blocking:
                ack = build_ack(message, "AE", blockers_text(blocking), error_code="207")
            else:
                ack = build_ack(message, "AA")
            saved = archive(payload, message, options.out) if options.out else None
            _log(options, message, len(payload), saved, "error" if blocking else "ok")
            for issue in blocking:
                print(f"  {issue}", file=sys.stderr)
        except Hl7Error as error:
            ack = build_minimal_ack(payload, text=str(error))
            saved = archive(payload, None, options.out) if options.out else None
            _log(options, None, len(payload), saved, "reject")
            print(f"  parse error: {error}", file=sys.stderr)

        if not options.no_ack:
            self.request.sendall(mllp_frame(ack.to_bytes()))


def blockers_text(issues) -> str:
    codes = ", ".join(sorted({item.code for item in issues}))
    return f"Rejected by validation: {codes}"


def _log(options, message: Message | None, size: int, saved: Path | None, status: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    if message is None:
        print(f"{stamp} status={status} bytes={size} file={saved or '-'}")
        return
    code, trigger, _structure = message.message_type
    print(
        f"{stamp} status={status} type={code}^{trigger} control_id={message.control_id or '-'} "
        f"bytes={size} file={saved or '-'}"
    )


def archive(payload: bytes, message: Message | None, directory: str) -> Path:
    target_dir = Path(directory)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    control_id = (message.control_id if message else "") or "unknown"
    safe_id = "".join(char if char.isalnum() or char in "-_" else "_" for char in control_id)[:40]
    name = f"{stamp}-{safe_id}-{uuid.uuid4().hex[:6]}.hl7"
    path = target_dir / name
    atomic_write(path, payload)
    return path


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def command_serve(args: argparse.Namespace) -> int:
    context = None
    if args.tls_cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(args.tls_cert, args.tls_key)
        if args.tls_ca:
            context.load_verify_locations(cafile=args.tls_ca)
            context.verify_mode = ssl.CERT_REQUIRED

    server = _Server((args.host, args.port), MllpHandler, ssl_context=context)
    server.options = args  # type: ignore[attr-defined]
    scheme = "mllps" if context else "mllp"
    print(f"listening on {scheme}://{args.host}:{args.port} out={args.out or '(no archive)'} "
          f"reject_warnings={args.reject_warnings}")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("shutting down")
    finally:
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HL7 v2.x parser, validator and MLLP tool")
    sub = parser.add_subparsers(dest="command", required=True)

    parse = sub.add_parser("parse", help="print message summary or full parse tree")
    parse.add_argument("file")
    parse.add_argument("--json", action="store_true", help="emit the full parsed structure")
    parse.add_argument("--tree", action="store_true", help="one line per segment (no field values)")
    parse.add_argument("--show-values", action="store_true", help="print segment text (may contain PHI)")
    parse.add_argument("--width", type=int, default=200)
    parse.add_argument("--encoding", default=None)
    parse.add_argument("--lenient", action="store_true", help="skip malformed lines instead of failing")
    parse.set_defaults(func=command_parse)

    valid = sub.add_parser("validate", help="structural and message-type checks")
    valid.add_argument("file")
    valid.add_argument("--json", action="store_true")
    valid.add_argument("--strict", action="store_true", help="treat warnings as errors")
    valid.add_argument("--encoding", default=None)
    valid.set_defaults(func=command_validate)

    ack = sub.add_parser("ack", help="build an acknowledgement for an inbound message")
    ack.add_argument("file")
    ack.add_argument("--code", default="AA", choices=["AA", "AE", "AR", "CA", "CE", "CR"])
    ack.add_argument("--text", default="", help="error text for AE/AR (never include patient values)")
    ack.add_argument("--echo-trigger", action="store_true")
    ack.add_argument("--out", default=None)
    ack.add_argument("--encoding", default=None)
    ack.set_defaults(func=command_ack)

    send = sub.add_parser("send", help="send one message over MLLP and check the ACK")
    send.add_argument("host")
    send.add_argument("port", type=int)
    send.add_argument("--file", required=True)
    send.add_argument("--timeout", type=float, default=10.0)
    send.add_argument("--tls", action="store_true")
    send.add_argument("--insecure", action="store_true", help="skip certificate verification (test only)")
    send.add_argument("--ca-file", default=None)
    send.add_argument("--cert-file", default=None)
    send.add_argument("--key-file", default=None)
    send.add_argument("--ack-out", default=None)
    send.add_argument("--json", action="store_true")
    send.add_argument("--encoding", default=None)
    send.set_defaults(func=command_send)

    serve = sub.add_parser("serve", help="MLLP listener that archives messages and answers ACKs")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=6661)
    serve.add_argument("--out", default=None, help="directory for received messages")
    serve.add_argument("--timeout", type=float, default=30.0)
    serve.add_argument("--no-ack", action="store_true")
    serve.add_argument("--reject-warnings", action="store_true")
    serve.add_argument("--tls-cert", default=None)
    serve.add_argument("--tls-key", default=None)
    serve.add_argument("--tls-ca", default=None, help="require client certificates signed by this CA")
    serve.set_defaults(func=command_serve)

    frame = sub.add_parser("frame", help="add MLLP framing to a raw message")
    frame.add_argument("file")
    frame.add_argument("-o", "--out", default=None)
    frame.set_defaults(func=command_frame)

    unframe = sub.add_parser("unframe", help="extract the payload of an MLLP capture")
    unframe.add_argument("file")
    unframe.add_argument("-o", "--out", default=None)
    unframe.set_defaults(func=command_unframe)

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
