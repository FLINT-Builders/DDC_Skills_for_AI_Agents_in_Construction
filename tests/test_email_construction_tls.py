"""SMTP certificate checks for the email-construction skill.

ConstructionEmailSender is loaded from the skill document and exercised
against a local impostor that completes the SMTP dialogue. These tests fail
while that sender still delivers the mailbox password and the message to a
peer that has not authenticated as the configured host. They pass once the
handshake verifies the certificate and the hostname before login or DATA.

Run from the repository root:

    python3 -m unittest discover -s tests -p 'test_*.py' -v
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = (
    REPO_ROOT
    / "4_DDC_Curated"
    / "Document-Generation"
    / "email-construction"
    / "SKILL.md"
)

SMTP_USER = "operator-smtp-user"
SMTP_PASSWORD = "operator-smtp-password-placeholder"
BODY_MARKER = "RFI-RESPONSE-BODY-MARKER"
ATTACHMENT_MARKER = "CHANGE-ORDER-ATTACHMENT-MARKER"
CONFIGURED_HOST = "127.0.0.1"


@dataclass
class PeerObservation:
    """What the SMTP peer actually received. Secrets are not retained."""

    connection_accepted: bool = False
    client_hello_seen: bool = False
    username_observed: bool = False
    password_observed: bool = False
    body_observed: bool = False
    attachment_observed: bool = False

    def __repr__(self) -> str:
        return (
            "PeerObservation("
            f"connection_accepted={self.connection_accepted}, "
            f"client_hello_seen={self.client_hello_seen}, "
            f"username_observed={self.username_observed}, "
            f"password_observed={self.password_observed}, "
            f"body_observed={self.body_observed}, "
            f"attachment_observed={self.attachment_observed})"
        )


def load_construction_email_sender():
    """Execute the skill's sender class exactly as the document defines it."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, flags=re.DOTALL)
    for block in blocks:
        if "class ConstructionEmailSender" not in block:
            continue
        namespace: dict = {}
        # The snippet annotates send() with List but does not import it.
        # The name is only needed so the class statement can execute.
        exec("from typing import List\n" + block, namespace, namespace)
        return namespace["ConstructionEmailSender"]
    raise AssertionError(f"ConstructionEmailSender not found in {SKILL_PATH}")


def _openssl(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["openssl", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _write_self_signed_for_host(directory: Path) -> tuple[Path, Path]:
    certificate = directory / "self.crt"
    key = directory / "self.key"
    _openssl(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-keyout",
        key.name,
        "-out",
        certificate.name,
        "-days",
        "1",
        "-nodes",
        "-subj",
        "/CN=127.0.0.1",
        "-addext",
        "subjectAltName=IP:127.0.0.1",
        cwd=directory,
    )
    return certificate, key


def _write_trusted_cert_for_other_name(directory: Path) -> tuple[Path, Path, Path]:
    """CA plus a certificate whose name is not the configured SMTP host."""
    ca_certificate = directory / "ca.crt"
    ca_key = directory / "ca.key"
    _openssl(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-keyout",
        ca_key.name,
        "-out",
        ca_certificate.name,
        "-days",
        "1",
        "-nodes",
        "-subj",
        "/CN=Email Construction Test CA",
        cwd=directory,
    )
    _openssl(
        "req",
        "-newkey",
        "rsa:2048",
        "-keyout",
        "other.key",
        "-out",
        "other.csr",
        "-nodes",
        "-subj",
        "/CN=attacker.invalid",
        cwd=directory,
    )
    (directory / "other.ext").write_text(
        "subjectAltName=DNS:attacker.invalid\n", encoding="ascii"
    )
    _openssl(
        "x509",
        "-req",
        "-in",
        "other.csr",
        "-CA",
        ca_certificate.name,
        "-CAkey",
        ca_key.name,
        "-CAcreateserial",
        "-out",
        "other.crt",
        "-days",
        "1",
        "-extfile",
        "other.ext",
        cwd=directory,
    )
    return ca_certificate, directory / "other.crt", directory / "other.key"


class _LineBuffer:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.pending = b""

    def readline(self) -> bytes:
        while b"\n" not in self.pending:
            chunk = self.sock.recv(4096)
            if not chunk:
                return b""
            self.pending += chunk
        line, self.pending = self.pending.split(b"\n", 1)
        return line + b"\n"


def _send(sock: socket.socket, text: str) -> None:
    sock.sendall(text.encode("ascii"))


def _ehlo_reply(include_starttls: bool) -> str:
    lines = ["250-impostor greets"]
    if include_starttls:
        lines.append("250-STARTTLS")
    lines.append("250-AUTH PLAIN")
    lines.append("250 HELP")
    return "\r\n".join(lines) + "\r\n"


def _note_application_bytes(blobs: list[bytes], data: bytes) -> None:
    if data:
        blobs.append(data)


def _decode_base64_tokens(raw: bytes) -> list[bytes]:
    found = []
    for token in re.findall(rb"[A-Za-z0-9+/]{8,}={0,2}", raw):
        try:
            found.append(base64.b64decode(token, validate=False))
        except Exception:
            continue
    return found


def _contains(blobs: list[bytes], needle: str) -> bool:
    raw = b"\n".join(blobs)
    haystacks = [raw, *_decode_base64_tokens(raw)]
    target = needle.encode("utf-8")
    return any(target in haystack for haystack in haystacks)


def _peek_byte(sock: socket.socket, timeout: float) -> bytes:
    sock.settimeout(timeout)
    try:
        return sock.recv(1, socket.MSG_PEEK)
    except socket.timeout:
        return b""


def _start_tls(sock: socket.socket, server_context: ssl.SSLContext, timeout: float):
    """Wrap sock if the peer starts a TLS handshake. Return (sock_or_none, hello_seen)."""
    peeked = _peek_byte(sock, timeout)
    if not peeked:
        return None, False
    if peeked[:1] != b"\x16":
        return sock, False
    sock.settimeout(5)
    try:
        return server_context.wrap_socket(sock, server_side=True), True
    except ssl.SSLError:
        # The client sent a handshake and then aborted, which is what a
        # verifying client does when this certificate is unacceptable.
        return None, True


def _read_message_data(reader: _LineBuffer, blobs: list[bytes]) -> None:
    while True:
        line = reader.readline()
        if line == b"":
            return
        _note_application_bytes(blobs, line)
        if line in (b".\r\n", b".\n"):
            return


def _serve_session(
    conn: socket.socket,
    server_context: ssl.SSLContext,
    blobs: list[bytes],
    hello_seen: list[bool],
) -> None:
    # Implicit TLS sends a ClientHello before the banner. STARTTLS waits
    # for the banner, so an empty peek here means speak plaintext SMTP.
    peeked = _peek_byte(conn, 0.2)
    if peeked[:1] == b"\x16":
        wrapped, seen = _start_tls(conn, server_context, timeout=5)
        hello_seen[0] = hello_seen[0] or seen
        if wrapped is None:
            conn.close()
            return
        conn = wrapped
        plaintext_phase = False
    else:
        plaintext_phase = True

    _send(conn, "220 impostor.example ESMTP\r\n")
    reader = _LineBuffer(conn)
    try:
        while True:
            line = reader.readline()
            if line == b"":
                break
            _note_application_bytes(blobs, line)
            command = line.decode("ascii", "replace").strip()
            verb = command.split(" ", 1)[0].upper()
            if verb in ("EHLO", "HELO"):
                _send(conn, _ehlo_reply(include_starttls=plaintext_phase))
            elif verb == "STARTTLS":
                _send(conn, "220 Ready to start TLS\r\n")
                wrapped, seen = _start_tls(conn, server_context, timeout=5)
                hello_seen[0] = hello_seen[0] or seen
                if wrapped is None or not seen:
                    break
                conn = wrapped
                reader = _LineBuffer(conn)
                plaintext_phase = False
            elif verb == "AUTH":
                if command.upper() == "AUTH PLAIN":
                    _send(conn, "334 \r\n")
                    challenge_response = reader.readline()
                    _note_application_bytes(blobs, challenge_response)
                _send(conn, "235 Authentication successful\r\n")
            elif verb == "MAIL":
                _send(conn, "250 OK\r\n")
            elif verb == "RCPT":
                _send(conn, "250 OK\r\n")
            elif verb == "DATA":
                _send(conn, "354 End data with <CR><LF>.<CR><LF>\r\n")
                _read_message_data(reader, blobs)
                _send(conn, "250 OK\r\n")
            elif verb == "QUIT":
                _send(conn, "221 Bye\r\n")
                break
            elif verb in ("RSET", "NOOP"):
                _send(conn, "250 OK\r\n")
            else:
                _send(conn, "500 unrecognized\r\n")
    finally:
        try:
            conn.close()
        except OSError:
            pass


def _observe_send(certificate: Path, key: Path, trust_ca: Path | None) -> PeerObservation:
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(str(certificate), str(key))
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((CONFIGURED_HOST, 0))
    listener.listen(5)
    port = listener.getsockname()[1]
    blobs: list[bytes] = []
    hello_seen = [False]
    accepted = [False]
    stop = threading.Event()

    def accept_loop() -> None:
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _addr = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            accepted[0] = True
            try:
                _serve_session(conn, server_context, blobs, hello_seen)
            except Exception:
                try:
                    conn.close()
                except OSError:
                    pass

    thread = threading.Thread(target=accept_loop, daemon=True)
    thread.start()
    previous_timeout = socket.getdefaulttimeout()
    previous_trust = os.environ.get("SSL_CERT_FILE")
    socket.setdefaulttimeout(5)
    if trust_ca is None:
        os.environ.pop("SSL_CERT_FILE", None)
    else:
        os.environ["SSL_CERT_FILE"] = str(trust_ca)
    try:
        sender_cls = load_construction_email_sender()
        with tempfile.TemporaryDirectory() as attachment_dir:
            attachment = Path(attachment_dir) / "change-order.pdf"
            attachment.write_bytes(ATTACHMENT_MARKER.encode("ascii"))
            sender = sender_cls(
                CONFIGURED_HOST,
                port,
                SMTP_USER,
                SMTP_PASSWORD,
            )
            try:
                sender.send(
                    ["field-engineer@example.test"],
                    {
                        "subject": "RFI response",
                        "body": BODY_MARKER,
                        "cc": ["project-manager@example.test"],
                        "priority": "high",
                        "attachments": [str(attachment)],
                    },
                )
            except Exception:
                # A verifying handshake raises before login. The peer record
                # decides whether anything was disclosed before that refusal.
                pass
    finally:
        socket.setdefaulttimeout(previous_timeout)
        if previous_trust is None:
            os.environ.pop("SSL_CERT_FILE", None)
        else:
            os.environ["SSL_CERT_FILE"] = previous_trust
        stop.set()
        thread.join(timeout=2)
        listener.close()

    return PeerObservation(
        connection_accepted=accepted[0],
        client_hello_seen=hello_seen[0],
        username_observed=_contains(blobs, SMTP_USER),
        password_observed=_contains(blobs, SMTP_PASSWORD),
        body_observed=_contains(blobs, BODY_MARKER),
        attachment_observed=_contains(blobs, ATTACHMENT_MARKER),
    )


class EmailConstructionTlsTests(unittest.TestCase):
    def test_self_signed_certificate_does_not_receive_credentials_or_mail(self):
        """A certificate that chains to no trust anchor must not receive mail."""
        openssl = shutil.which("openssl")
        if openssl is None:
            self.fail("openssl is required to mint the impostor certificate")
        with tempfile.TemporaryDirectory() as directory:
            certificate, key = _write_self_signed_for_host(Path(directory))
            observed = _observe_send(certificate, key, trust_ca=None)
        self.assertTrue(
            observed.connection_accepted,
            "sender never connected to the configured SMTP host and port",
        )
        self.assertTrue(
            observed.client_hello_seen,
            "sender never started a TLS handshake with the configured peer",
        )
        self.assertFalse(
            observed.username_observed,
            "SMTP peer observed the mailbox username",
        )
        self.assertFalse(
            observed.password_observed,
            "SMTP peer observed the mailbox password",
        )
        self.assertFalse(
            observed.body_observed,
            "SMTP peer observed the message body",
        )
        self.assertFalse(
            observed.attachment_observed,
            "SMTP peer observed the attachment",
        )

    def test_trusted_certificate_for_another_name_does_not_receive_mail(self):
        """A trusted certificate issued to a different name must not receive mail.

        The certificate is signed by a CA placed in SSL_CERT_FILE and names
        attacker.invalid, not the host passed to ConstructionEmailSender.
        """
        openssl = shutil.which("openssl")
        if openssl is None:
            self.fail("openssl is required to mint the impostor certificate")
        with tempfile.TemporaryDirectory() as directory:
            ca_certificate, certificate, key = _write_trusted_cert_for_other_name(
                Path(directory)
            )
            observed = _observe_send(certificate, key, trust_ca=ca_certificate)
        self.assertTrue(
            observed.connection_accepted,
            "sender never connected to the configured SMTP host and port",
        )
        self.assertTrue(
            observed.client_hello_seen,
            "sender never started a TLS handshake with the configured peer",
        )
        self.assertFalse(
            observed.username_observed,
            "SMTP peer observed the mailbox username",
        )
        self.assertFalse(
            observed.password_observed,
            "SMTP peer observed the mailbox password",
        )
        self.assertFalse(
            observed.body_observed,
            "SMTP peer observed the message body",
        )
        self.assertFalse(
            observed.attachment_observed,
            "SMTP peer observed the attachment",
        )


if __name__ == "__main__":
    unittest.main()
