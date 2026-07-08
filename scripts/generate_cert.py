"""Browser-based self-signed TLS certificate generator for VVV."""

from __future__ import annotations

import http.server
import json
import stat
import webbrowser
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

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
        + '<div class="label">Start server with TLS:</div>'
        + '<pre>uvicorn server.app:app --host 0.0.0.0 --port 8443 \\\\\n'
        + '  --ssl-certfile ' + data.cert_path + ' \\\\\n'
        + '  --ssl-keyfile ' + data.key_path + '</pre>'
        + '<div class="label">Connect client with self-signed cert:</div>'
        + '<pre>vvv --server https://HOST:8443 --ca-cert ' + data.cert_path
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

    out.mkdir(parents=True, exist_ok=True)

    private_key = ec.generate_private_key(ec.SECP256R1())

    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, hostname),
        ]
    )

    san = x509.SubjectAlternativeName(
        [
            x509.DNSName(hostname),
            x509.DNSName("localhost"),
            x509.IPAddress(__import__("ipaddress").IPv4Address("127.0.0.1")),
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

    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    key_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_path.write_bytes(key_bytes)
    key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0o600

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
            body: dict[str, Any] = json.loads(self.rfile.read(int(length_str)))

            missing = [f for f in ("hostname", "days", "certs_dir") if f not in body]
            if missing:
                error_msg = f"Missing required fields: {', '.join(missing)}"
                self._send_response(
                    400, "application/json", json.dumps({"error": error_msg}).encode()
                )
                return

            result = _generate_cert(
                hostname=body["hostname"],
                days=int(body["days"]),
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
