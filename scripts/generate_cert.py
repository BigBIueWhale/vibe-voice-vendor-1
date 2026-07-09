"""Browser-based self-signed TLS certificate generator for VVV."""

from __future__ import annotations

import http.server
import ipaddress
import json
import os
import stat
import webbrowser
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

_CERT_MODE = stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH
_DIR_MODE = stat.S_IRWXU
_KEY_MODE = stat.S_IRUSR | stat.S_IWUSR
_MAX_GENERATE_BODY_BYTES = 4096
_MAX_HOSTNAME_BYTES = 253
_MAX_VALIDITY_DAYS = 3650

_HTML_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VVV Certificate Generator</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, -apple-system, sans-serif; background: #f5f5f5;
         color: #333; display: flex; justify-content: center; padding: 2rem; }
  .container { background: #fff; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1);
               padding: 2rem; max-width: 520px; width: 100%; }
  h1 { font-size: 1.4rem; margin-bottom: 1.5rem; }
  label { display: block; font-weight: 600; margin-bottom: 0.3rem; font-size: 0.9rem; }
  input { width: 100%; padding: 0.5rem; border: 1px solid #ccc; border-radius: 4px;
          font-size: 0.95rem; margin-bottom: 1rem; }
  button { background: #2563eb; color: #fff; border: none; border-radius: 4px;
           padding: 0.6rem 1.2rem; font-size: 1rem; cursor: pointer; width: 100%; }
  button:hover { background: #1d4ed8; }
  button:disabled { background: #93c5fd; cursor: not-allowed; }
  #result { margin-top: 1.5rem; display: none; }
  .success { background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 4px;
             padding: 1rem; }
  .error { background: #fef2f2; border: 1px solid #fecaca; border-radius: 4px;
           padding: 1rem; color: #991b1b; }
  pre { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 4px;
        padding: 0.5rem; font-size: 0.82rem; overflow-x: auto; margin-top: 0.5rem;
        white-space: pre-wrap; word-break: break-all; }
  .label { font-weight: 600; font-size: 0.85rem; margin-top: 0.8rem; }
</style>
</head>
<body>
<div class="container">
  <h1>VVV Certificate Generator</h1>
  <form id="form">
    <label for="hostname">Hostname</label>
    <input id="hostname" name="hostname" placeholder="Enter hostname" required>

    <label for="days">Validity (days)</label>
    <input id="days" name="days" type="number" placeholder="Enter validity (days)" min="1" required>

    <label for="certs_dir">Output directory</label>
    <input id="certs_dir" name="certs_dir" placeholder="Enter output directory path" required>

    <button type="submit" id="btn">Generate Certificate</button>
  </form>

  <div id="result"></div>
</div>
<script>
document.getElementById("form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = document.getElementById("btn");
  const result = document.getElementById("result");
  btn.disabled = true;
  btn.textContent = "Generating\u2026";
  result.style.display = "none";
  try {
    const resp = await fetch("/generate", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        hostname: document.getElementById("hostname").value,
        days: parseInt(document.getElementById("days").value, 10),
        certs_dir: document.getElementById("certs_dir").value,
      }),
    });
    const data = await resp.json();
    if (data.error) {
      result.innerHTML = '<div class="error">' + data.error + '</div>';
    } else {
      result.innerHTML = '<div class="success">'
        + '<div class="label">Certificate:</div><pre>' + data.cert_path + '</pre>'
        + '<div class="label">Private key:</div><pre>' + data.key_path + '</pre>'
        + '<div class="label">Use with the Rust TLS proxy:</div>'
        + '<pre>./rust_proxy/target/release/vvv_proxy \\\\\n'
        + '  --upstream-uds "$HOME/.config/vibevoice-vendor/run/server.sock" \\\\\n'
        + '  --listen-host 0.0.0.0 \\\\\n'
        + '  --listen-port 42862 \\\\\n'
        + '  --max-body-size 525336576 \\\\\n'
        + '  --jwt-public-key-file ./keys/public.pem \\\\\n'
        + '  --revoked-tokens-file ./revoked_tokens.txt \\\\\n'
        + '  --cert-path ' + data.cert_path + ' \\\\\n'
        + '  --key-path ' + data.key_path + ' \\\\\n'
        + '  --client-ca-cert-path ./certs/self-signed/client-ca.pem \\\\\n'
        + '  --cert-validity-days 3650 \\\\\n'
        + '  --cert-check-interval-secs 3600</pre>'
        + '<div class="label">Connect client with self-signed cert:</div>'
        + '<pre>vvv --server https://HOST:42862 --token TOKEN --ca-cert ' + data.cert_path
        + ' --client-cert ./keys/client-cert.pem --client-key ./keys/client-key.pem'
        + ' transcribe audio.wav</pre>'
        + '</div>';
    }
    result.style.display = "block";
  } catch (err) {
    result.innerHTML = '<div class="error">Request failed: ' + err.message + '</div>';
    result.style.display = "block";
  } finally {
    btn.disabled = false;
    btn.textContent = "Generate Certificate";
  }
});
</script>
</body>
</html>
"""


def _write_secret(path: Path, data: bytes) -> None:
    fd = _open_create_exclusive(path, _KEY_MODE)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    path.chmod(_KEY_MODE)


def _write_public(path: Path, data: bytes) -> None:
    fd = _open_create_exclusive(path, _CERT_MODE)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    path.chmod(_CERT_MODE)


def _open_create_exclusive(path: Path, mode: int) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.open(path, flags, mode)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)


def _validate_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"{path} is a symlink")
    if not path.is_dir():
        raise ValueError(f"{path} is not a directory")
    mode = _mode(path)
    if mode != _DIR_MODE:
        raise ValueError(f"{path} mode is {mode:03o}, expected 700")


def _create_or_validate_directory(path: Path) -> None:
    if path.exists():
        _validate_directory(path)
    else:
        path.mkdir(parents=True)
        path.chmod(_DIR_MODE)
        _validate_directory(path)


def _validate_days(days: int) -> None:
    if not 1 <= days <= _MAX_VALIDITY_DAYS:
        raise ValueError(f"days must be between 1 and {_MAX_VALIDITY_DAYS}")


def _hostname_san(hostname: str) -> x509.GeneralName:
    try:
        return x509.IPAddress(ipaddress.ip_address(hostname))
    except ValueError:
        pass

    labels = hostname.split(".")
    for label in labels:
        if not 1 <= len(label) <= 63:
            raise ValueError("hostname contains an invalid DNS label length")
        if label.startswith("-") or label.endswith("-"):
            raise ValueError("hostname labels must not start or end with '-'")
        if not all(char.isascii() and (char.isalnum() or char == "-") for char in label):
            raise ValueError("hostname must contain only ASCII letters, digits, '-' and '.'")
    return x509.DNSName(hostname)


def _validate_hostname(hostname: str) -> x509.GeneralName:
    if not hostname:
        raise ValueError("hostname must not be empty")
    if len(hostname.encode("utf-8")) > _MAX_HOSTNAME_BYTES:
        raise ValueError(f"hostname exceeds {_MAX_HOSTNAME_BYTES} bytes")
    if hostname != hostname.strip():
        raise ValueError("hostname must not have leading or trailing whitespace")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in hostname):
        raise ValueError("hostname must not contain control characters")
    return _hostname_san(hostname)


def _generate_cert(
    hostname: str,
    days: int,
    certs_dir: str,
) -> dict[str, str]:
    """Generate a self-signed ECDSA certificate and return file paths.

    Returns a dict with ``cert_path`` and ``key_path`` on success,
    or ``error`` on failure.
    """
    out = Path(certs_dir)
    cert_path = out / "fullchain.pem"
    key_path = out / "privkey.pem"

    if cert_path.exists() or key_path.exists():
        return {"error": f"Certificate files already exist in {out}. Remove them first."}

    try:
        _validate_days(days)
        primary_san = _validate_hostname(hostname)
        _create_or_validate_directory(out)
    except ValueError as exc:
        return {"error": str(exc)}

    private_key = ec.generate_private_key(ec.SECP256R1())

    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, hostname),
        ]
    )

    san = x509.SubjectAlternativeName(
        [
            primary_san,
            x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
        ]
    )

    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=days))
        .add_extension(san, critical=False)
        .sign(private_key, hashes.SHA256())
    )

    _write_public(cert_path, cert.public_bytes(serialization.Encoding.PEM))

    key_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    _write_secret(key_path, key_bytes)

    return {
        "cert_path": str(cert_path),
        "key_path": str(key_path),
    }


class _RequestHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/":
            self._send_response(200, "text/html", _HTML_PAGE.encode())
        else:
            self._send_response(404, "text/plain", b"Not found")

    def do_POST(self) -> None:
        if self.path == "/generate":
            length_str = self.headers.get("Content-Length")
            if length_str is None:
                err = json.dumps({"error": "Missing Content-Length"}).encode()
                self._send_response(400, "application/json", err)
                return
            try:
                length = int(length_str)
            except ValueError:
                err = json.dumps({"error": "Invalid Content-Length"}).encode()
                self._send_response(400, "application/json", err)
                return
            if length < 0 or length > _MAX_GENERATE_BODY_BYTES:
                err = json.dumps({"error": "Request body is too large"}).encode()
                self._send_response(413, "application/json", err)
                return
            try:
                parsed = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                err = json.dumps({"error": "Invalid JSON"}).encode()
                self._send_response(400, "application/json", err)
                return
            if not isinstance(parsed, dict):
                err = json.dumps({"error": "JSON body must be an object"}).encode()
                self._send_response(400, "application/json", err)
                return
            body: dict[str, Any] = parsed

            missing = [f for f in ("hostname", "days", "certs_dir") if f not in body]
            if missing:
                error_msg = f"Missing required fields: {', '.join(missing)}"
                self._send_response(
                    400, "application/json", json.dumps({"error": error_msg}).encode()
                )
                return
            if not isinstance(body["hostname"], str) or not isinstance(body["certs_dir"], str):
                err = json.dumps({"error": "hostname and certs_dir must be strings"}).encode()
                self._send_response(400, "application/json", err)
                return
            if not isinstance(body["days"], int):
                err = json.dumps({"error": "days must be an integer"}).encode()
                self._send_response(400, "application/json", err)
                return

            result = _generate_cert(
                hostname=body["hostname"],
                days=body["days"],
                certs_dir=body["certs_dir"],
            )
            data = json.dumps(result)
            self._send_response(200, "application/json", data.encode())
        else:
            self._send_response(404, "text/plain", b"Not found")

    def _send_response(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass  # Silence request logging


def main() -> None:
    server = http.server.HTTPServer(("127.0.0.1", 0), _RequestHandler)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    print(f"Certificate generator running at {url}")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
