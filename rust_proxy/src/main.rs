//! VibeVoice TLS Reverse Proxy
//!
//! Self-contained, zero-install SSL-terminating reverse proxy for the
//! VibeVoice ASR server. Generates self-signed certificates automatically
//! and hot-reloads them on expiry with zero downtime.
//!
//! All configuration is provided via required CLI arguments — no defaults.

use std::collections::HashSet;
use std::convert::Infallible;
use std::fs;
use std::io::{self, Write};
use std::net::SocketAddr;
use std::panic::AssertUnwindSafe;
use std::path::{Path, PathBuf};
use std::sync::{Arc, RwLock};
use std::time::Duration;

use base64::{engine::general_purpose::STANDARD, Engine as _};
use bytes::Bytes;
use clap::Parser;
use futures::FutureExt;
use http::{header, HeaderMap, HeaderName, HeaderValue, Method, Request, Response, StatusCode};
use http_body::Body as HttpBody;
use http_body_util::{combinators::UnsyncBoxBody, BodyExt, Full, Limited};
use hyper::body::Incoming;
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper_util::rt::{TokioIo, TokioTimer};
use ring::digest;
use rustls::pki_types::{pem::PemObject, CertificateDer, PrivateKeyDer};
use rustls::server::WebPkiClientVerifier;
use rustls::RootCertStore;
use tokio::net::{TcpListener, TcpSocket, TcpStream, UnixStream};
use tokio::signal;
use tokio::sync::{OwnedSemaphorePermit, Semaphore};
use tokio::task::JoinSet;
use tokio_rustls::TlsAcceptor;
use tracing::{debug, error, info, warn, Level};
use x509_parser::pem::Pem;

// ============================================================================
// CLI Arguments (all required, no defaults)
// ============================================================================

#[derive(Parser)]
#[command(name = "vvv_proxy", about = "VibeVoice TLS Reverse Proxy")]
struct Args {
    /// Upstream server Unix domain socket path
    #[arg(long)]
    upstream_uds: PathBuf,

    /// Expected Unix peer UID for the upstream UDS server
    #[arg(long)]
    upstream_peer_uid: u32,

    /// Expected Unix peer GID for the upstream UDS server
    #[arg(long)]
    upstream_peer_gid: u32,

    /// HTTPS listen host (e.g., 0.0.0.0)
    #[arg(long)]
    listen_host: String,

    /// HTTPS listen port (e.g., 42862)
    #[arg(long)]
    listen_port: u16,

    /// Maximum request body size in bytes (e.g., 524288000 for 500 MB)
    #[arg(long)]
    max_body_size: usize,

    /// Path to TLS certificate PEM file
    #[arg(long)]
    cert_path: String,

    /// Path to TLS private key PEM file
    #[arg(long)]
    key_path: String,

    /// Path to write the public sha256/<base64 SPKI> server identity pin
    #[arg(long)]
    server_spki_pin_path: String,

    /// Path to client CA certificate PEM file for mandatory mTLS
    #[arg(long)]
    client_ca_cert_path: String,

    /// Certificate validity in days for self-signed generation (e.g., 3650)
    #[arg(long)]
    cert_validity_days: u32,

    /// Certificate expiry check interval in seconds (e.g., 3600)
    #[arg(long)]
    cert_check_interval_secs: u64,
}

// ============================================================================
// Hop-by-Hop Header Constants
// ============================================================================

/// Hop-by-hop headers stripped during proxying (RFC 7230 Section 6.1).
const HOP_BY_HOP_HEADERS: &[&str] = &[
    "connection",
    "keep-alive",
    "transfer-encoding",
    "te",
    "trailers",
    "upgrade",
    "proxy-authorization",
    "proxy-authenticate",
    "proxy-connection",
];

fn connection_header_tokens(headers: &HeaderMap) -> HashSet<HeaderName> {
    let mut tokens = HashSet::new();
    for value in headers.get_all(header::CONNECTION) {
        let Ok(raw) = value.to_str() else {
            continue;
        };
        for token in raw.split(',') {
            let token = token.trim();
            if token.is_empty() {
                continue;
            }
            if let Ok(name) = HeaderName::from_bytes(token.as_bytes()) {
                tokens.insert(name);
            }
        }
    }
    tokens
}

fn is_hop_by_hop_header(name: &HeaderName, connection_tokens: &HashSet<HeaderName>) -> bool {
    HOP_BY_HOP_HEADERS
        .iter()
        .any(|header| name.as_str().eq_ignore_ascii_case(header))
        || connection_tokens.contains(name)
}

// ============================================================================
// Public Transport Limits
// ============================================================================

const TLS_HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(3);
const HTTP1_HEADER_READ_TIMEOUT: Duration = Duration::from_secs(5);
const HTTP1_MAX_HEADERS: usize = 32;
const HTTP1_MAX_BUFFER_BYTES: usize = 32 * 1024;
const MAX_PUBLIC_CONNECTIONS: usize = 128;
const TCP_LISTEN_BACKLOG: u32 = MAX_PUBLIC_CONNECTIONS as u32;
const TLS_ALPN_HTTP1: &[u8] = b"http/1.1";
const UPSTREAM_CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
const SERVER_CERT_COMMON_NAME: &str = "VVV Sovereign Server";
const SERVER_SPKI_PIN_PREFIX: &str = "sha256/";
const CLIENT_IDENTITY_HEADER: &str = "x-vvv-client-spki-sha256";

type BoxError = Box<dyn std::error::Error + Send + Sync>;
type ProxyBody = UnsyncBoxBody<Bytes, BoxError>;
type SharedTlsConfig = Arc<RwLock<Arc<rustls::ServerConfig>>>;

// ============================================================================
// Security Headers
// ============================================================================

/// Security headers injected into every proxied response.
/// Matches the original Caddyfile policy plus HSTS for HTTPS enforcement.
fn security_headers() -> [(HeaderName, HeaderValue); 4] {
    [
        (
            HeaderName::from_static("strict-transport-security"),
            HeaderValue::from_static("max-age=63072000; includeSubDomains; preload"),
        ),
        (
            HeaderName::from_static("x-content-type-options"),
            HeaderValue::from_static("nosniff"),
        ),
        (
            HeaderName::from_static("x-frame-options"),
            HeaderValue::from_static("DENY"),
        ),
        (
            HeaderName::from_static("referrer-policy"),
            HeaderValue::from_static("no-referrer"),
        ),
    ]
}

fn apply_security_headers(headers: &mut HeaderMap) {
    for (name, value) in security_headers() {
        headers.insert(name, value);
    }
}

fn boxed_full_body(bytes: Bytes) -> ProxyBody {
    Full::new(bytes)
        .map_err(|err: Infallible| match err {})
        .boxed_unsync()
}

fn empty_response_body() -> ProxyBody {
    boxed_full_body(Bytes::new())
}

fn static_response_body(body: &'static [u8]) -> ProxyBody {
    boxed_full_body(Bytes::from_static(body))
}

fn text_response(status: StatusCode, body: &'static str) -> Response<ProxyBody> {
    let mut response = Response::new(static_response_body(body.as_bytes()));
    *response.status_mut() = status;
    apply_security_headers(response.headers_mut());
    response
}

fn local_health_response(method: &Method) -> Response<ProxyBody> {
    match *method {
        Method::GET => {
            let mut response =
                Response::new(static_response_body(br#"{"status":"ok","proxy":"ok"}"#));
            response.headers_mut().insert(
                header::CONTENT_TYPE,
                HeaderValue::from_static("application/json"),
            );
            apply_security_headers(response.headers_mut());
            response
        }
        Method::HEAD => {
            let mut response = Response::new(empty_response_body());
            apply_security_headers(response.headers_mut());
            response
        }
        _ => text_response(StatusCode::METHOD_NOT_ALLOWED, "Method Not Allowed"),
    }
}

fn make_shared_tls_config(config: rustls::ServerConfig) -> SharedTlsConfig {
    Arc::new(RwLock::new(Arc::new(config)))
}

fn current_tls_config(tls_config: &SharedTlsConfig) -> io::Result<Arc<rustls::ServerConfig>> {
    tls_config
        .read()
        .map(|config| Arc::clone(&*config))
        .map_err(|_| io::Error::other("TLS config lock poisoned"))
}

fn bind_public_listener(addr: SocketAddr) -> io::Result<std::net::TcpListener> {
    if !addr.is_ipv4() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "public listener must be an IPv4 socket address",
        ));
    }
    let socket = TcpSocket::new_v4()?;
    socket.set_reuseaddr(true)?;
    socket.bind(addr)?;
    socket.listen(TCP_LISTEN_BACKLOG)?.into_std()
}

fn try_public_connection_permit(permits: &Arc<Semaphore>) -> io::Result<OwnedSemaphorePermit> {
    permits.clone().try_acquire_owned().map_err(|_| {
        io::Error::new(
            io::ErrorKind::ConnectionRefused,
            "public connection limit reached",
        )
    })
}

fn write_private_file(path: &Path, contents: &[u8]) -> io::Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o600)
            .open(path)?;
        file.write_all(contents)?;
        file.sync_all()?;
        fs::set_permissions(path, fs::Permissions::from_mode(0o600))?;
        Ok(())
    }

    #[cfg(not(unix))]
    {
        fs::write(path, contents)
    }
}

fn write_public_file(path: &Path, contents: &[u8]) -> io::Result<()> {
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .open(path)?;
    file.write_all(contents)?;
    file.sync_all()?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o644))?;
    }
    Ok(())
}

// ============================================================================
// Self-Signed Certificate Management
// ============================================================================

/// Generate a self-signed ECDSA P-256 certificate and write it to disk.
/// Creates parent directories if they do not exist.
fn generate_self_signed_cert(
    cert_path: &Path,
    key_path: &Path,
    validity_days: u32,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    use rcgen::{CertificateParams, DnType, KeyPair, PKCS_ECDSA_P256_SHA256};

    info!("Generating self-signed server identity certificate");

    if let Some(parent) = cert_path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("Failed to create cert directory {}: {e}", parent.display()))?;
    }
    if let Some(parent) = key_path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("Failed to create key directory {}: {e}", parent.display()))?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(parent, std::fs::Permissions::from_mode(0o700)).map_err(
                |e| {
                    format!(
                        "Failed to restrict key directory permissions on {}: {e}",
                        parent.display()
                    )
                },
            )?;
        }
    }

    let mut params = CertificateParams::default();
    params
        .distinguished_name
        .push(DnType::CommonName, SERVER_CERT_COMMON_NAME);
    params.subject_alt_names = Vec::new();

    let now = time::OffsetDateTime::now_utc();
    params.not_before = now;
    params.not_after = now + time::Duration::days(i64::from(validity_days));

    let key_pair = if key_path.exists() {
        let metadata = fs::symlink_metadata(key_path)
            .map_err(|e| format!("Failed to inspect key {}: {e}", key_path.display()))?;
        if metadata.file_type().is_symlink() || !metadata.file_type().is_file() {
            return Err(format!("Server key {} must be a regular file", key_path.display()).into());
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = metadata.permissions().mode() & 0o777;
            if mode != 0o600 {
                return Err(format!(
                    "Server key {} mode is {mode:03o}, expected 600",
                    key_path.display()
                )
                .into());
            }
        }
        let pem = fs::read_to_string(key_path)
            .map_err(|e| format!("Failed to read existing key {}: {e}", key_path.display()))?;
        KeyPair::from_pem(&pem).map_err(|e| {
            format!(
                "Failed to parse existing server key {}: {e}",
                key_path.display()
            )
        })?
    } else {
        let key_pair = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256)
            .map_err(|e| format!("Failed to generate ECDSA P-256 key pair: {e}"))?;
        write_private_file(key_path, key_pair.serialize_pem().as_bytes())
            .map_err(|e| format!("Failed to write key to {}: {e}", key_path.display()))?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(key_path, std::fs::Permissions::from_mode(0o600)).map_err(
                |e| {
                    format!(
                        "Failed to restrict key permissions on {}: {e}",
                        key_path.display()
                    )
                },
            )?;
        }
        key_pair
    };
    let cert = params
        .self_signed(&key_pair)
        .map_err(|e| format!("Failed to self-sign certificate: {e}"))?;

    std::fs::write(cert_path, cert.pem())
        .map_err(|e| format!("Failed to write cert to {}: {e}", cert_path.display()))?;

    info!(
        cert = %cert_path.display(),
        key = %key_path.display(),
        valid_days = validity_days,
        "Self-signed server identity certificate generated"
    );

    Ok(())
}

fn server_spki_pin_from_cert_path(
    cert_path: &Path,
) -> Result<String, Box<dyn std::error::Error + Send + Sync>> {
    let cert_data = fs::read(cert_path)?;
    for pem in Pem::iter_from_buffer(&cert_data) {
        let pem = pem.map_err(|e| format!("invalid certificate PEM: {e:?}"))?;
        if pem.label != "CERTIFICATE" {
            continue;
        }
        let x509 = pem
            .parse_x509()
            .map_err(|e| format!("invalid X.509 server certificate: {e:?}"))?;
        let spki_hash = digest::digest(&digest::SHA256, x509.tbs_certificate.subject_pki.raw);
        return Ok(format!(
            "{SERVER_SPKI_PIN_PREFIX}{}",
            STANDARD.encode(spki_hash.as_ref())
        ));
    }
    Err("server certificate PEM does not contain a CERTIFICATE block".into())
}

fn write_server_spki_pin(
    cert_path: &Path,
    pin_path: &Path,
) -> Result<String, Box<dyn std::error::Error + Send + Sync>> {
    let pin = server_spki_pin_from_cert_path(cert_path)?;
    if let Some(parent) = pin_path.parent() {
        fs::create_dir_all(parent)
            .map_err(|e| format!("Failed to create pin directory {}: {e}", parent.display()))?;
    }
    write_public_file(pin_path, format!("{pin}\n").as_bytes()).map_err(|e| {
        format!(
            "Failed to write server SPKI pin to {}: {e}",
            pin_path.display()
        )
    })?;
    Ok(pin)
}

/// Check whether a PEM certificate file is still valid.
/// Returns `Some(remaining_duration)` if valid, `None` if expired or unreadable.
fn check_cert_expiry(cert_path: &Path) -> Option<Duration> {
    let cert_data = std::fs::read(cert_path).ok()?;
    let pem = Pem::iter_from_buffer(&cert_data).next()?.ok()?;
    let x509 = pem.parse_x509().ok()?;
    let common_name = x509
        .subject()
        .iter_common_name()
        .next()
        .and_then(|cn| cn.as_str().ok())?;
    if common_name != SERVER_CERT_COMMON_NAME {
        return None;
    }
    if x509.subject_alternative_name().ok().flatten().is_some() {
        return None;
    }
    let remaining = x509.validity().time_to_expiration()?;
    let secs = remaining.whole_seconds();
    if secs < 0 {
        return None;
    }
    Some(Duration::new(
        secs as u64,
        remaining.subsec_nanoseconds() as u32,
    ))
}

fn load_pem_certificate_chain(
    path: &Path,
) -> Result<Vec<CertificateDer<'static>>, Box<dyn std::error::Error + Send + Sync>> {
    let data = fs::read(path)
        .map_err(|e| format!("Failed to read certificate file {}: {e}", path.display()))?;
    let certs = CertificateDer::pem_slice_iter(&data)
        .collect::<Result<Vec<_>, _>>()
        .map_err(|_| format!("Failed to parse certificate PEM {}", path.display()))?;
    if certs.is_empty() {
        return Err(format!(
            "Certificate file {} contained no certificates",
            path.display()
        )
        .into());
    }
    Ok(certs)
}

fn load_single_pem_private_key(
    path: &Path,
) -> Result<PrivateKeyDer<'static>, Box<dyn std::error::Error + Send + Sync>> {
    let data = fs::read(path)
        .map_err(|e| format!("Failed to read private key file {}: {e}", path.display()))?;
    let mut key_result: Option<PrivateKeyDer<'static>> = None;
    for item in PrivateKeyDer::pem_slice_iter(&data) {
        let key =
            item.map_err(|_| format!("Failed to parse private key PEM {}", path.display()))?;
        if key_result.is_some() {
            return Err(format!(
                "Private key file {} contained multiple keys",
                path.display()
            )
            .into());
        }
        key_result = Some(key);
    }
    key_result
        .ok_or_else(|| format!("Private key file {} contained no keys", path.display()).into())
}

fn load_client_root_store(
    path: &Path,
) -> Result<RootCertStore, Box<dyn std::error::Error + Send + Sync>> {
    let certs = load_pem_certificate_chain(path)?;
    let mut roots = RootCertStore::empty();
    let (valid, invalid) = roots.add_parsable_certificates(certs);
    if valid == 0 || invalid != 0 {
        return Err(format!(
            "Client CA file {} had {valid} valid and {invalid} invalid certificates",
            path.display()
        )
        .into());
    }
    Ok(roots)
}

fn build_tls_config(
    cert_path: &Path,
    key_path: &Path,
    client_ca_cert_path: &Path,
) -> Result<rustls::ServerConfig, Box<dyn std::error::Error + Send + Sync>> {
    let cert_chain = load_pem_certificate_chain(cert_path)?;
    let key = load_single_pem_private_key(key_path)?;
    let client_roots = load_client_root_store(client_ca_cert_path)?;
    let client_verifier = WebPkiClientVerifier::builder(Arc::new(client_roots))
        .build()
        .map_err(|e| format!("Failed to build mTLS client verifier: {e}"))?;

    let mut config =
        rustls::ServerConfig::builder_with_protocol_versions(&[&rustls::version::TLS13])
            .with_client_cert_verifier(client_verifier)
            .with_single_cert(cert_chain, key)
            .map_err(|e| format!("Failed to build TLS 1.3-only server config: {e}"))?;
    config.alpn_protocols = vec![TLS_ALPN_HTTP1.to_vec()];
    Ok(config)
}

/// Background task: periodically check certificate expiry and hot-reload if expired.
async fn cert_renewal_task(
    cert_path: PathBuf,
    key_path: PathBuf,
    server_spki_pin_path: PathBuf,
    client_ca_cert_path: PathBuf,
    validity_days: u32,
    check_interval_secs: u64,
    tls_config: SharedTlsConfig,
) {
    let interval = Duration::from_secs(check_interval_secs);
    loop {
        tokio::time::sleep(interval).await;

        if check_cert_expiry(&cert_path).is_some() {
            continue;
        }

        warn!("Certificate expired or unreadable — regenerating");
        if let Err(e) = generate_self_signed_cert(&cert_path, &key_path, validity_days) {
            error!(error = %e, "Certificate regeneration failed");
            continue;
        }
        match write_server_spki_pin(&cert_path, &server_spki_pin_path) {
            Ok(pin) => info!(pin = %pin, "Server SPKI pin refreshed"),
            Err(e) => {
                error!(error = %e, "Failed to refresh server SPKI pin");
                continue;
            }
        }
        match build_tls_config(&cert_path, &key_path, &client_ca_cert_path) {
            Ok(config) => match tls_config.write() {
                Ok(mut shared_config) => {
                    *shared_config = Arc::new(config);
                    info!("Certificate hot-reloaded successfully (zero downtime)");
                }
                Err(_) => {
                    error!("Certificate hot-reload failed: TLS config lock poisoned");
                }
            },
            Err(e) => error!(error = %e, "Certificate hot-reload failed"),
        }
    }
}

// ============================================================================
// mTLS Client Identity
// ============================================================================

fn spki_sha256_hex_from_cert_der(
    cert_der: &[u8],
) -> Result<String, Box<dyn std::error::Error + Send + Sync>> {
    let (_, cert) = x509_parser::parse_x509_certificate(cert_der)
        .map_err(|e| format!("invalid X.509 client certificate: {e:?}"))?;
    let hash = digest::digest(&digest::SHA256, cert.tbs_certificate.subject_pki.raw);
    Ok(hex_lower(hash.as_ref()))
}

fn hex_lower(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push(HEX[(byte >> 4) as usize] as char);
        out.push(HEX[(byte & 0x0f) as usize] as char);
    }
    out
}

fn client_identity_from_peer_certs(
    peer_certs: Option<&[CertificateDer<'_>]>,
) -> Result<String, &'static str> {
    let certs = peer_certs.ok_or("mTLS peer certificate chain missing")?;
    let leaf = certs.first().ok_or("mTLS peer certificate chain empty")?;
    spki_sha256_hex_from_cert_der(leaf.as_ref()).map_err(|_| "invalid mTLS client certificate")
}

// ============================================================================
// Application State
// ============================================================================

const UDS_UPSTREAM_AUTHORITY: &str = "vvv-upstream";

#[derive(Clone)]
struct UpstreamTarget {
    socket_path: PathBuf,
    host_header: String,
    expected_peer_uid: u32,
    expected_peer_gid: u32,
}

impl UpstreamTarget {
    fn from_args(args: &Args) -> Self {
        Self {
            socket_path: args.upstream_uds.clone(),
            host_header: UDS_UPSTREAM_AUTHORITY.to_string(),
            expected_peer_uid: args.upstream_peer_uid,
            expected_peer_gid: args.upstream_peer_gid,
        }
    }

    fn host_header(&self) -> &str {
        &self.host_header
    }
}

fn validate_upstream_socket_path(upstream: &UpstreamTarget) -> io::Result<()> {
    use std::os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt};

    if !upstream.socket_path.is_absolute() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "upstream UDS path must be absolute",
        ));
    }

    let parent = upstream.socket_path.parent().ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            "upstream UDS path must have a parent directory",
        )
    })?;
    let parent_meta = fs::symlink_metadata(parent)?;
    let parent_type = parent_meta.file_type();
    if parent_type.is_symlink() || !parent_type.is_dir() {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "upstream UDS parent must be a real directory, not a symlink",
        ));
    }
    if parent_meta.uid() != upstream.expected_peer_uid
        || parent_meta.gid() != upstream.expected_peer_gid
    {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "upstream UDS parent owner does not match expected peer",
        ));
    }
    if parent_meta.permissions().mode() & 0o777 != 0o700 {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "upstream UDS parent must have mode 0700",
        ));
    }

    let socket_meta = fs::symlink_metadata(&upstream.socket_path)?;
    let socket_type = socket_meta.file_type();
    if socket_type.is_symlink() || !socket_type.is_socket() {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "upstream UDS path must be a real socket, not a symlink",
        ));
    }
    if socket_meta.uid() != upstream.expected_peer_uid
        || socket_meta.gid() != upstream.expected_peer_gid
    {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "upstream UDS owner does not match expected peer",
        ));
    }
    if socket_meta.permissions().mode() & 0o777 != 0o600 {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "upstream UDS must have mode 0600",
        ));
    }
    Ok(())
}

fn verify_upstream_peer(stream: &UnixStream, upstream: &UpstreamTarget) -> io::Result<()> {
    let cred = stream.peer_cred()?;
    if cred.uid() != upstream.expected_peer_uid || cred.gid() != upstream.expected_peer_gid {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "upstream UDS peer credentials do not match expected server identity",
        ));
    }
    Ok(())
}

async fn connect_upstream_uds(upstream: &UpstreamTarget) -> io::Result<UnixStream> {
    validate_upstream_socket_path(upstream)?;
    let stream = UnixStream::connect(&upstream.socket_path).await?;
    verify_upstream_peer(&stream, upstream)?;
    Ok(stream)
}

#[derive(Clone)]
struct AppState {
    upstream: UpstreamTarget,
    max_body_size: usize,
}

impl AppState {
    fn new(upstream: UpstreamTarget, max_body_size: usize) -> Self {
        Self {
            upstream,
            max_body_size,
        }
    }
}

// ============================================================================
// Proxy Handlers
// ============================================================================

/// Top-level request handler. Wrapped in `catch_unwind` so that a
/// panic in any single request does not crash the entire server.
async fn proxy_handler<B>(
    state: AppState,
    client_addr: SocketAddr,
    client_identity: String,
    req: Request<B>,
) -> Response<ProxyBody>
where
    B: HttpBody<Data = Bytes> + Send + 'static,
    B::Error: Into<BoxError>,
{
    let result = AssertUnwindSafe(proxy_handler_inner(
        state,
        client_addr,
        client_identity,
        req,
    ))
    .catch_unwind()
    .await;

    match result {
        Ok(response) => response,
        Err(panic_info) => {
            let msg = panic_info
                .downcast_ref::<&str>()
                .map(|s| s.to_string())
                .or_else(|| panic_info.downcast_ref::<String>().cloned())
                .unwrap_or_else(|| "unknown panic payload".to_string());
            error!(panic = %msg, "Panic caught in request handler");
            text_response(StatusCode::INTERNAL_SERVER_ERROR, "Internal server error")
        }
    }
}

async fn proxy_handler_inner<B>(
    state: AppState,
    client_addr: SocketAddr,
    client_identity: String,
    req: Request<B>,
) -> Response<ProxyBody>
where
    B: HttpBody<Data = Bytes> + Send + 'static,
    B::Error: Into<BoxError>,
{
    if req.headers().contains_key(header::AUTHORIZATION) {
        return text_response(
            StatusCode::BAD_REQUEST,
            "Authorization header is not accepted",
        );
    }

    if req.uri().path() == "/health" {
        return local_health_response(req.method());
    }

    if is_http_upgrade(req.headers()) {
        return text_response(StatusCode::NOT_FOUND, "Not Found");
    }

    http_proxy(state, req, client_addr, &client_identity).await
}

fn is_http_upgrade(headers: &HeaderMap) -> bool {
    headers.contains_key(header::UPGRADE)
}

fn is_public_upstream_route(method: &Method, path: &str) -> bool {
    matches!(
        (method, path),
        (&Method::POST, "/v1/transcribe") | (&Method::GET, "/v1/queue/status")
    )
}

/// Reverse-proxy an HTTP request to the upstream FastAPI server.
/// Streams the response body back without buffering (critical for SSE).
async fn http_proxy<B>(
    state: AppState,
    req: Request<B>,
    client_addr: SocketAddr,
    client_identity: &str,
) -> Response<ProxyBody>
where
    B: HttpBody<Data = Bytes> + Send + 'static,
    B::Error: Into<BoxError>,
{
    let method = req.method().clone();
    let uri = req.uri().clone();
    let path_query = uri.path_and_query().map(|pq| pq.as_str()).unwrap_or("/");
    if !is_public_upstream_route(&method, uri.path()) {
        return text_response(StatusCode::NOT_FOUND, "Not Found");
    }

    debug!(method = %method, path = %path_query, client = %client_addr, "Proxying HTTP");

    if let Some(content_length) = req
        .headers()
        .get(header::CONTENT_LENGTH)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.parse::<u64>().ok())
    {
        if content_length > state.max_body_size as u64 {
            return text_response(StatusCode::PAYLOAD_TOO_LARGE, "Payload Too Large");
        }
    }

    // Build upstream headers: strip hop-by-hop, inject forwarding metadata.
    let mut upstream_headers = HeaderMap::new();
    let request_connection_tokens = connection_header_tokens(req.headers());
    for (key, value) in req.headers() {
        if !is_hop_by_hop_header(key, &request_connection_tokens)
            && key.as_str() != CLIENT_IDENTITY_HEADER
        {
            upstream_headers.append(key.clone(), value.clone());
        }
    }
    if let Ok(host_val) = HeaderValue::from_str(state.upstream.host_header()) {
        upstream_headers.insert(header::HOST, host_val);
    }
    if let Ok(ip_val) = HeaderValue::from_str(&client_addr.ip().to_string()) {
        upstream_headers.insert(HeaderName::from_static("x-forwarded-for"), ip_val.clone());
        upstream_headers.insert(HeaderName::from_static("x-real-ip"), ip_val);
    }
    upstream_headers.insert(
        HeaderName::from_static("x-forwarded-proto"),
        HeaderValue::from_static("https"),
    );
    if let Ok(identity_val) = HeaderValue::from_str(client_identity) {
        upstream_headers.insert(
            HeaderName::from_static(CLIENT_IDENTITY_HEADER),
            identity_val,
        );
    } else {
        error!(client = %client_addr, "Invalid mTLS client identity");
        return text_response(StatusCode::BAD_GATEWAY, "Bad Gateway");
    }

    // Stream request body to upstream without buffering (avoids holding up to 500 MB in memory).
    let limited_body = Limited::new(req.into_body(), state.max_body_size);
    // Remove Content-Length since the body is now streamed with chunked encoding.
    upstream_headers.remove(header::CONTENT_LENGTH);

    let upstream_uri = match path_query.parse::<hyper::Uri>() {
        Ok(uri) => uri,
        Err(e) => {
            error!(path = %path_query, client = %client_addr, error = %e, "Invalid upstream URI");
            return text_response(StatusCode::BAD_GATEWAY, "Bad Gateway");
        }
    };
    let mut upstream_request = match hyper::Request::builder()
        .method(method)
        .uri(upstream_uri)
        .body(limited_body)
    {
        Ok(req) => req,
        Err(e) => {
            error!(path = %path_query, client = %client_addr, error = %e, "Failed to build upstream request");
            return text_response(StatusCode::BAD_GATEWAY, "Bad Gateway");
        }
    };
    *upstream_request.headers_mut() = upstream_headers;

    let stream = match tokio::time::timeout(
        UPSTREAM_CONNECT_TIMEOUT,
        connect_upstream_uds(&state.upstream),
    )
    .await
    {
        Ok(Ok(stream)) => stream,
        Ok(Err(e)) => {
            error!(
                upstream = %state.upstream.socket_path.display(),
                client = %client_addr,
                error = %e,
                "Upstream UDS connect failed"
            );
            return text_response(StatusCode::BAD_GATEWAY, "Bad Gateway");
        }
        Err(_) => {
            error!(
                upstream = %state.upstream.socket_path.display(),
                client = %client_addr,
                "Upstream UDS connect timed out"
            );
            return text_response(StatusCode::BAD_GATEWAY, "Bad Gateway");
        }
    };

    let io = TokioIo::new(stream);
    let (mut sender, connection) = match hyper::client::conn::http1::handshake(io).await {
        Ok(parts) => parts,
        Err(e) => {
            error!(
                upstream = %state.upstream.socket_path.display(),
                client = %client_addr,
                error = %e,
                "Upstream HTTP/1 handshake failed"
            );
            return text_response(StatusCode::BAD_GATEWAY, "Bad Gateway");
        }
    };
    tokio::spawn(async move {
        if let Err(e) = connection.await {
            debug!(error = %e, "Upstream HTTP/1 connection ended with error");
        }
    });

    let upstream_response = match sender.send_request(upstream_request).await {
        Ok(response) => response,
        Err(e) => {
            error!(
                upstream = %state.upstream.socket_path.display(),
                client = %client_addr,
                error = %e,
                "Upstream request failed"
            );
            return text_response(StatusCode::BAD_GATEWAY, "Bad Gateway");
        }
    };
    let (upstream_parts, upstream_body) = upstream_response.into_parts();

    // Build the client-facing response.
    let status = upstream_parts.status;
    let mut response_headers = HeaderMap::new();

    // Copy upstream headers first. Strip hop-by-hop, content-length for streamed
    // bodies, and server (privacy). Use append() to preserve multiple Set-Cookie
    // headers.
    let response_connection_tokens = connection_header_tokens(&upstream_parts.headers);
    for (key, value) in &upstream_parts.headers {
        if !is_hop_by_hop_header(key, &response_connection_tokens)
            && key != header::CONTENT_LENGTH
            && key != header::SERVER
        {
            response_headers.append(key.clone(), value.clone());
        }
    }

    // Inject security headers last — insert() replaces any upstream duplicates,
    // ensuring the proxy's security policy always wins.
    for (name, value) in security_headers() {
        response_headers.insert(name, value);
    }

    // Stream the body without buffering; critical for SSE (flush_interval -1).
    let body = upstream_body
        .map_err(|err| -> BoxError { Box::new(err) })
        .boxed_unsync();
    let mut response = Response::new(body);
    *response.status_mut() = status;
    *response.headers_mut() = response_headers;
    response
}

// ============================================================================
// Server Setup
// ============================================================================

async fn serve_public(
    listener: std::net::TcpListener,
    tls_config: SharedTlsConfig,
    state: AppState,
) -> io::Result<()> {
    listener.set_nonblocking(true)?;
    let listener = TcpListener::from_std(listener)?;
    let permits = Arc::new(Semaphore::new(MAX_PUBLIC_CONNECTIONS));
    let mut tasks = JoinSet::new();
    let shutdown = shutdown_signal();
    tokio::pin!(shutdown);

    loop {
        drain_finished_public_tasks(&mut tasks);
        tokio::select! {
            _ = &mut shutdown => {
                break;
            }
            accept_result = listener.accept() => {
                let (tcp_stream, client_addr) = match accept_result {
                    Ok(accepted) => accepted,
                    Err(err) => {
                        error!(error = %err, "Public TCP accept failed");
                        tokio::time::sleep(Duration::from_millis(100)).await;
                        continue;
                    }
                };
                let permit = match try_public_connection_permit(&permits) {
                    Ok(permit) => permit,
                    Err(err) => {
                        debug!(client = %client_addr, error = %err, "Rejected public connection");
                        continue;
                    }
                };

                let tls_config = tls_config.clone();
                let state = state.clone();
                tasks.spawn(async move {
                    serve_public_connection(tcp_stream, client_addr, tls_config, state, permit).await;
                });
            }
            Some(result) = tasks.join_next(), if !tasks.is_empty() => {
                log_public_task_result(result, "Public connection task failed");
            }
        }
    }

    let drain = async {
        while let Some(result) = tasks.join_next().await {
            if let Err(err) = result {
                error!(error = %err, "Public connection task failed during shutdown");
            }
        }
    };
    if tokio::time::timeout(Duration::from_secs(10), drain)
        .await
        .is_err()
    {
        warn!("Timed out draining public connections; aborting remaining tasks");
    }
    Ok(())
}

fn drain_finished_public_tasks(tasks: &mut JoinSet<()>) {
    while let Some(result) = tasks.try_join_next() {
        log_public_task_result(result, "Public connection task failed");
    }
}

fn log_public_task_result(result: Result<(), tokio::task::JoinError>, message: &'static str) {
    if let Err(err) = result {
        error!(error = %err, "{}", message);
    }
}

async fn serve_public_connection(
    tcp_stream: TcpStream,
    client_addr: SocketAddr,
    tls_config: SharedTlsConfig,
    state: AppState,
    _permit: OwnedSemaphorePermit,
) {
    let config = match current_tls_config(&tls_config) {
        Ok(config) => config,
        Err(err) => {
            error!(client = %client_addr, error = %err, "Cannot load TLS config");
            return;
        }
    };
    let acceptor = TlsAcceptor::from(config);
    let tls_stream =
        match tokio::time::timeout(TLS_HANDSHAKE_TIMEOUT, acceptor.accept(tcp_stream)).await {
            Ok(Ok(stream)) => stream,
            Ok(Err(err)) => {
                debug!(client = %client_addr, error = %err, "TLS handshake failed");
                return;
            }
            Err(_) => {
                debug!(client = %client_addr, "TLS handshake timed out");
                return;
            }
        };
    let client_identity =
        match client_identity_from_peer_certs(tls_stream.get_ref().1.peer_certificates()) {
            Ok(identity) => identity,
            Err(err) => {
                debug!(client = %client_addr, error = %err, "mTLS client identity unavailable");
                return;
            }
        };

    let service = service_fn(move |req: Request<Incoming>| {
        let state = state.clone();
        let client_identity = client_identity.clone();
        async move { Ok::<_, Infallible>(proxy_handler(state, client_addr, client_identity, req).await) }
    });

    let mut builder = http1::Builder::new();
    builder
        .timer(TokioTimer::new())
        .header_read_timeout(HTTP1_HEADER_READ_TIMEOUT)
        .max_headers(HTTP1_MAX_HEADERS)
        .max_buf_size(HTTP1_MAX_BUFFER_BYTES)
        .keep_alive(false);

    if let Err(err) = builder
        .serve_connection(TokioIo::new(tls_stream), service)
        .await
    {
        debug!(client = %client_addr, error = %err, "Public HTTP/1 connection ended with error");
    }
}

/// Wait for Ctrl+C or SIGTERM.
async fn shutdown_signal() {
    let ctrl_c = async {
        signal::ctrl_c()
            .await
            .expect("Failed to install Ctrl+C signal handler");
    };

    #[cfg(unix)]
    let terminate = async {
        signal::unix::signal(signal::unix::SignalKind::terminate())
            .expect("Failed to install SIGTERM signal handler")
            .recv()
            .await;
    };

    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => {}
        _ = terminate => {}
    }

    info!("Shutdown signal received, draining connections");
}

// ============================================================================
// Entry Point
// ============================================================================

#[tokio::main]
async fn main() {
    let args = Args::parse();

    rustls::crypto::ring::default_provider()
        .install_default()
        .expect("Failed to install rustls ring crypto provider");

    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::from_default_env().add_directive(Level::INFO.into()),
        )
        .init();

    let cert_path = PathBuf::from(&args.cert_path);
    let key_path = PathBuf::from(&args.key_path);
    let server_spki_pin_path = PathBuf::from(&args.server_spki_pin_path);
    let client_ca_cert_path = PathBuf::from(&args.client_ca_cert_path);

    // Ensure a valid certificate exists before starting the server.
    match check_cert_expiry(&cert_path) {
        Some(remaining) => {
            let days = remaining.as_secs() / 86400;
            info!(
                cert = %cert_path.display(),
                remaining_days = days,
                "Using existing certificate"
            );
        }
        None => {
            generate_self_signed_cert(&cert_path, &key_path, args.cert_validity_days)
                .unwrap_or_else(|e| panic!("Failed to generate initial certificate: {e}"));
        }
    }
    let server_spki_pin =
        write_server_spki_pin(&cert_path, &server_spki_pin_path).unwrap_or_else(|e| {
            panic!(
                "Failed to write server SPKI pin from cert={} to {}: {e}",
                cert_path.display(),
                server_spki_pin_path.display()
            )
        });

    let tls_config = make_shared_tls_config(
        build_tls_config(&cert_path, &key_path, &client_ca_cert_path).unwrap_or_else(|e| {
            panic!(
                "Failed to load mTLS config from cert={}, key={}, client_ca={}: {e}",
                cert_path.display(),
                key_path.display(),
                client_ca_cert_path.display()
            )
        }),
    );

    // Background task: regenerate and hot-reload the cert if it ever expires.
    tokio::spawn(cert_renewal_task(
        cert_path.clone(),
        key_path.clone(),
        server_spki_pin_path.clone(),
        client_ca_cert_path.clone(),
        args.cert_validity_days,
        args.cert_check_interval_secs,
        tls_config.clone(),
    ));

    let upstream = UpstreamTarget::from_args(&args);

    let state = AppState::new(upstream.clone(), args.max_body_size);

    let listen_addr: SocketAddr = format!("{}:{}", args.listen_host, args.listen_port)
        .parse()
        .unwrap_or_else(|e| {
            panic!(
                "Invalid listen address {}:{}: {e}",
                args.listen_host, args.listen_port
            )
        });

    info!("VibeVoice TLS Proxy ready");
    info!("  HTTPS: https://{}:{}", args.listen_host, args.listen_port);
    info!("  Server SPKI pin: {}", server_spki_pin);
    info!("  Upstream UDS: {}", upstream.socket_path.display());

    let listener = bind_public_listener(listen_addr)
        .unwrap_or_else(|e| panic!("Failed to bind public TCP listener on {listen_addr}: {e}"));

    serve_public(listener, tls_config, state)
        .await
        .unwrap_or_else(|e| panic!("HTTPS server failed on {listen_addr}: {e}"));

    info!("Reverse proxy stopped");
}

#[cfg(test)]
mod tests {
    use super::*;
    use http_body::Frame;
    use http_body_util::Empty;
    use std::os::unix::fs::{MetadataExt, PermissionsExt};
    use std::pin::Pin;
    use std::process;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::task::{Context, Poll};
    use std::time::{SystemTime, UNIX_EPOCH};

    const TEST_CLIENT_IDENTITY: &str =
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

    struct TestFiles {
        dir: PathBuf,
    }

    impl TestFiles {
        fn new() -> Self {
            let unique = format!(
                "vvv-proxy-test-{}-{}",
                process::id(),
                SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .expect("system clock before unix epoch")
                    .as_nanos()
            );
            let dir = std::env::temp_dir().join(unique);
            fs::create_dir(&dir).expect("create test temp dir");
            fs::set_permissions(&dir, fs::Permissions::from_mode(0o700))
                .expect("set test temp dir permissions");
            Self { dir }
        }

        fn state(&self) -> AppState {
            AppState::new(
                UpstreamTarget {
                    socket_path: self.dir.join("missing-upstream.sock"),
                    host_header: UDS_UPSTREAM_AUTHORITY.to_string(),
                    expected_peer_uid: self.uid(),
                    expected_peer_gid: self.gid(),
                },
                1024,
            )
        }

        fn uid(&self) -> u32 {
            self.dir.metadata().expect("test dir metadata").uid()
        }

        fn gid(&self) -> u32 {
            self.dir.metadata().expect("test dir metadata").gid()
        }
    }

    impl Drop for TestFiles {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.dir);
        }
    }

    struct CountingBody {
        polls: Arc<AtomicUsize>,
    }

    impl HttpBody for CountingBody {
        type Data = Bytes;
        type Error = Infallible;

        fn poll_frame(
            self: Pin<&mut Self>,
            _cx: &mut Context<'_>,
        ) -> Poll<Option<Result<Frame<Self::Data>, Self::Error>>> {
            self.polls.fetch_add(1, Ordering::SeqCst);
            Poll::Ready(Some(Ok(Frame::data(Bytes::from_static(
                b"body must not be polled",
            )))))
        }
    }

    fn body_that_counts_polls(polls: Arc<AtomicUsize>) -> CountingBody {
        CountingBody { polls }
    }

    fn empty_request_body() -> Empty<Bytes> {
        Empty::new()
    }

    async fn response_body_bytes(response: Response<ProxyBody>, limit: usize) -> Bytes {
        let bytes = response
            .into_body()
            .collect()
            .await
            .expect("read response body")
            .to_bytes();
        assert!(
            bytes.len() <= limit,
            "response body length {} exceeded test limit {limit}",
            bytes.len()
        );
        bytes
    }

    fn write_test_client_ca_cert(path: &Path) {
        use rcgen::{
            BasicConstraints, CertificateParams, DnType, IsCa, KeyPair, PKCS_ECDSA_P256_SHA256,
        };

        let mut params = CertificateParams::default();
        params
            .distinguished_name
            .push(DnType::CommonName, "vvv-test-client-ca");
        params.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
        let key_pair = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).expect("generate CA key");
        let cert = params.self_signed(&key_pair).expect("self-sign CA");
        fs::write(path, cert.pem()).expect("write client CA cert");
    }

    #[tokio::test]
    async fn proxy_public_listener_binds_with_explicit_backlog_path() {
        let listener = bind_public_listener(SocketAddr::from(([127, 0, 0, 1], 0)))
            .expect("bind public listener");
        let addr = listener.local_addr().expect("listener local addr");
        assert_eq!(
            addr.ip(),
            std::net::IpAddr::V4(std::net::Ipv4Addr::LOCALHOST)
        );
        assert_ne!(addr.port(), 0);
    }

    #[test]
    fn proxy_public_listener_rejects_ipv6() {
        let err = bind_public_listener("[::1]:0".parse().expect("IPv6 socket address"))
            .expect_err("IPv6 public listener rejected");
        assert_eq!(err.kind(), io::ErrorKind::InvalidInput);
    }

    #[cfg(unix)]
    #[test]
    fn generated_tls_identity_has_private_key_and_public_pin() {
        use std::os::unix::fs::PermissionsExt;

        let unique = format!(
            "vvv-proxy-cert-test-{}-{}",
            process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .expect("system clock before unix epoch")
                .as_nanos()
        );
        let dir = std::env::temp_dir().join(unique);
        let cert_path = dir.join("fullchain.pem");
        let key_path = dir.join("privkey.pem");
        let pin_path = dir.join("server-spki-pin.txt");

        generate_self_signed_cert(&cert_path, &key_path, 1).expect("generate cert");
        let pin = write_server_spki_pin(&cert_path, &pin_path).expect("write SPKI pin");
        let original_key = fs::read_to_string(&key_path).expect("read generated key");

        assert_eq!(
            dir.metadata().expect("dir metadata").permissions().mode() & 0o777,
            0o700
        );
        assert_eq!(
            key_path
                .metadata()
                .expect("key metadata")
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
        assert!(pin.starts_with(SERVER_SPKI_PIN_PREFIX));
        assert_eq!(
            fs::read_to_string(&pin_path).expect("read pin file"),
            format!("{pin}\n")
        );
        assert_eq!(
            pin_path
                .metadata()
                .expect("pin metadata")
                .permissions()
                .mode()
                & 0o777,
            0o644
        );

        let cert_data = fs::read(&cert_path).expect("read cert");
        let pem = Pem::iter_from_buffer(&cert_data)
            .next()
            .expect("one cert")
            .expect("valid cert pem");
        let cert = pem.parse_x509().expect("parse cert");
        let common_name = cert
            .subject()
            .iter_common_name()
            .next()
            .and_then(|cn| cn.as_str().ok())
            .expect("common name");
        assert_eq!(common_name, SERVER_CERT_COMMON_NAME);
        assert!(cert
            .subject_alternative_name()
            .expect("valid SAN state")
            .is_none());

        generate_self_signed_cert(&cert_path, &key_path, 2).expect("renew cert");
        let renewed_pin = write_server_spki_pin(&cert_path, &pin_path).expect("write renewed pin");
        assert_eq!(
            fs::read_to_string(&key_path).expect("read renewed key"),
            original_key
        );
        assert_eq!(renewed_pin, pin);

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn tls_config_requires_client_ca_tls13_and_http1_only_alpn() {
        let _ = rustls::crypto::ring::default_provider().install_default();
        let unique = format!(
            "vvv-proxy-mtls-test-{}-{}",
            process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .expect("system clock before unix epoch")
                .as_nanos()
        );
        let dir = std::env::temp_dir().join(unique);
        let cert_path = dir.join("fullchain.pem");
        let key_path = dir.join("privkey.pem");
        let client_ca_path = dir.join("client-ca.pem");

        generate_self_signed_cert(&cert_path, &key_path, 1).expect("generate server cert");
        assert!(build_tls_config(&cert_path, &key_path, &client_ca_path).is_err());

        write_test_client_ca_cert(&client_ca_path);
        let config =
            build_tls_config(&cert_path, &key_path, &client_ca_path).expect("build mTLS config");
        assert_eq!(config.alpn_protocols, vec![b"http/1.1".to_vec()]);

        let _ = fs::remove_dir_all(&dir);
    }

    #[tokio::test]
    async fn public_connection_limit_rejects_excess_connections_without_waiting() {
        let permits = Arc::new(Semaphore::new(1));
        let permit = try_public_connection_permit(&permits).expect("first connection accepted");

        let err = match try_public_connection_permit(&permits) {
            Ok(_) => panic!("second connection should be rejected while permit is held"),
            Err(err) => err,
        };
        assert_eq!(err.kind(), io::ErrorKind::ConnectionRefused);

        drop(permit);
        let _permit = try_public_connection_permit(&permits)
            .expect("connection accepted after permit release");
    }

    #[tokio::test]
    async fn proxy_health_stays_local_after_mtls() {
        let files = TestFiles::new();
        let req = Request::builder()
            .method(Method::GET)
            .uri("/health")
            .body(empty_request_body())
            .expect("request");

        let response = proxy_handler_inner(
            files.state(),
            SocketAddr::from(([127, 0, 0, 1], 12345)),
            TEST_CLIENT_IDENTITY.to_string(),
            req,
        )
        .await;

        assert_eq!(response.status(), StatusCode::OK);
        let body = response_body_bytes(response, 1024).await;
        assert_eq!(&body[..], br#"{"status":"ok","proxy":"ok"}"#);
    }

    #[tokio::test]
    async fn proxy_rejects_authorization_header_without_polling_body() {
        let files = TestFiles::new();
        let body_polls = Arc::new(AtomicUsize::new(0));
        let req = Request::builder()
            .method(Method::POST)
            .uri("/v1/transcribe")
            .header(header::AUTHORIZATION, "forbidden")
            .body(body_that_counts_polls(body_polls.clone()))
            .expect("request");

        let response = proxy_handler_inner(
            files.state(),
            SocketAddr::from(([127, 0, 0, 1], 12345)),
            TEST_CLIENT_IDENTITY.to_string(),
            req,
        )
        .await;

        assert_eq!(response.status(), StatusCode::BAD_REQUEST);
        assert_eq!(body_polls.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn proxy_does_not_send_continue_for_authorization_expect_request() {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};

        let files = TestFiles::new();
        let state = files.state();
        let (mut client_io, server_io) = tokio::io::duplex(4096);
        let service = service_fn(move |req: Request<Incoming>| {
            let state = state.clone();
            async move {
                Ok::<_, Infallible>(
                    proxy_handler(
                        state,
                        SocketAddr::from(([127, 0, 0, 1], 12345)),
                        TEST_CLIENT_IDENTITY.to_string(),
                        req,
                    )
                    .await,
                )
            }
        });

        let server = tokio::spawn(async move {
            let mut builder = http1::Builder::new();
            builder
                .timer(TokioTimer::new())
                .header_read_timeout(HTTP1_HEADER_READ_TIMEOUT)
                .max_headers(HTTP1_MAX_HEADERS)
                .max_buf_size(HTTP1_MAX_BUFFER_BYTES)
                .keep_alive(false);
            builder
                .serve_connection(TokioIo::new(server_io), service)
                .await
        });

        client_io
            .write_all(
                b"POST /v1/transcribe HTTP/1.1\r\n\
                  Host: example.test\r\n\
                  Authorization: forbidden\r\n\
                  Expect: 100-continue\r\n\
                  Content-Length: 4\r\n\r\n",
            )
            .await
            .expect("write public request");

        let mut response = Vec::new();
        tokio::time::timeout(Duration::from_secs(1), client_io.read_to_end(&mut response))
            .await
            .expect("server response timeout")
            .expect("read response");
        let response = String::from_utf8_lossy(&response);
        assert!(response.starts_with("HTTP/1.1 400 Bad Request"));
        assert!(!response.contains("100 Continue"));

        server
            .await
            .expect("server task")
            .expect("HTTP/1 connection");
    }

    #[tokio::test]
    async fn proxy_rejects_oversized_content_length() {
        let files = TestFiles::new();
        let req = Request::builder()
            .method(Method::POST)
            .uri("/v1/transcribe")
            .header(header::CONTENT_LENGTH, "1025")
            .body(empty_request_body())
            .expect("request");

        let response = proxy_handler_inner(
            files.state(),
            SocketAddr::from(([127, 0, 0, 1], 12345)),
            TEST_CLIENT_IDENTITY.to_string(),
            req,
        )
        .await;

        assert_eq!(response.status(), StatusCode::PAYLOAD_TOO_LARGE);
    }

    #[tokio::test]
    async fn proxy_rejects_http_upgrade_without_upstream() {
        let files = TestFiles::new();
        let body_polls = Arc::new(AtomicUsize::new(0));
        let req = Request::builder()
            .method(Method::POST)
            .uri("/v1/transcribe")
            .header(header::UPGRADE, "h2c")
            .body(body_that_counts_polls(body_polls.clone()))
            .expect("request");

        let response = proxy_handler_inner(
            files.state(),
            SocketAddr::from(([127, 0, 0, 1], 12345)),
            TEST_CLIENT_IDENTITY.to_string(),
            req,
        )
        .await;

        assert_eq!(response.status(), StatusCode::NOT_FOUND);
        assert_eq!(body_polls.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn proxy_rejects_unknown_route_without_upstream() {
        let files = TestFiles::new();
        let body_polls = Arc::new(AtomicUsize::new(0));
        let req = Request::builder()
            .method(Method::POST)
            .uri("/v1/not-real")
            .body(body_that_counts_polls(body_polls.clone()))
            .expect("request");

        let response = proxy_handler_inner(
            files.state(),
            SocketAddr::from(([127, 0, 0, 1], 12345)),
            TEST_CLIENT_IDENTITY.to_string(),
            req,
        )
        .await;

        assert_eq!(response.status(), StatusCode::NOT_FOUND);
        assert_eq!(body_polls.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn proxy_rejects_wrong_method_without_upstream() {
        let files = TestFiles::new();
        let req = Request::builder()
            .method(Method::GET)
            .uri("/v1/transcribe")
            .body(empty_request_body())
            .expect("request");

        let response = proxy_handler_inner(
            files.state(),
            SocketAddr::from(([127, 0, 0, 1], 12345)),
            TEST_CLIENT_IDENTITY.to_string(),
            req,
        )
        .await;

        assert_eq!(response.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn proxy_forwards_mtls_identity_http_over_unix_socket() {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        use tokio::net::UnixListener;

        let files = TestFiles::new();
        let socket_path = files.dir.join("upstream.sock");
        let listener = UnixListener::bind(&socket_path).expect("bind test UDS");
        fs::set_permissions(&socket_path, fs::Permissions::from_mode(0o600))
            .expect("set test UDS permissions");
        let upstream = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.expect("accept UDS request");
            let mut buf = [0_u8; 4096];
            let n = stream.read(&mut buf).await.expect("read UDS request");
            let request = String::from_utf8_lossy(&buf[..n]);
            assert!(request.starts_with("GET /v1/queue/status HTTP/1.1"));
            let request_lower = request.to_ascii_lowercase();
            assert!(!request_lower.contains("connection:"));
            assert!(!request_lower.contains("authorization:"));
            assert!(!request_lower.contains("x-strip-request:"));
            assert!(!request_lower
                .contains("ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"));
            assert!(request_lower.contains("x-keep-request: visible"));
            assert!(request_lower.contains(&format!(
                "{}: {}",
                CLIENT_IDENTITY_HEADER, TEST_CLIENT_IDENTITY
            )));
            stream
                .write_all(
                    b"HTTP/1.1 200 OK\r\n\
                      connection: x-strip-response\r\n\
                      x-strip-response: secret\r\n\
                      x-keep-response: visible\r\n\
                      server: private-backend\r\n\
                      content-length: 2\r\n\r\nok",
                )
                .await
                .expect("write UDS response");
        });

        let state = AppState::new(
            UpstreamTarget {
                socket_path,
                host_header: UDS_UPSTREAM_AUTHORITY.to_string(),
                expected_peer_uid: files.uid(),
                expected_peer_gid: files.gid(),
            },
            1024,
        );
        let req = Request::builder()
            .method(Method::GET)
            .uri("/v1/queue/status")
            .header(
                CLIENT_IDENTITY_HEADER,
                "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
            )
            .header(header::CONNECTION, "keep-alive, x-strip-request")
            .header("x-strip-request", "secret")
            .header("x-keep-request", "visible")
            .body(empty_request_body())
            .expect("request");

        let response = proxy_handler_inner(
            state,
            SocketAddr::from(([127, 0, 0, 1], 12345)),
            TEST_CLIENT_IDENTITY.to_string(),
            req,
        )
        .await;
        assert_eq!(response.status(), StatusCode::OK);
        assert!(response.headers().get("x-strip-response").is_none());
        assert!(response.headers().get(header::CONNECTION).is_none());
        assert!(response.headers().get(header::SERVER).is_none());
        assert_eq!(
            response.headers().get("x-keep-response"),
            Some(&HeaderValue::from_static("visible"))
        );
        let body = response_body_bytes(response, 1024).await;
        assert_eq!(body.as_ref(), b"ok");
        upstream.await.expect("upstream task");
    }

    #[test]
    fn proxy_rejects_symlinked_upstream_socket_path() {
        use std::os::unix::fs::symlink;
        use std::os::unix::net::UnixListener;

        let files = TestFiles::new();
        let real_socket_path = files.dir.join("real.sock");
        let listener = UnixListener::bind(&real_socket_path).expect("bind real UDS");
        fs::set_permissions(&real_socket_path, fs::Permissions::from_mode(0o600))
            .expect("set real UDS permissions");
        let symlink_path = files.dir.join("server.sock");
        symlink(&real_socket_path, &symlink_path).expect("create UDS symlink");
        let upstream = UpstreamTarget {
            socket_path: symlink_path,
            host_header: UDS_UPSTREAM_AUTHORITY.to_string(),
            expected_peer_uid: files.uid(),
            expected_peer_gid: files.gid(),
        };

        let err = validate_upstream_socket_path(&upstream).expect_err("symlink rejected");
        assert_eq!(err.kind(), io::ErrorKind::PermissionDenied);
        drop(listener);
    }

    #[tokio::test]
    async fn proxy_rejects_wrong_upstream_peer_credentials_before_http() {
        let files = TestFiles::new();
        let (client, _server) = UnixStream::pair().expect("UDS pair");
        let unexpected_uid = files.uid().checked_add(1).unwrap_or(0);
        let upstream = UpstreamTarget {
            socket_path: files.dir.join("unused.sock"),
            host_header: UDS_UPSTREAM_AUTHORITY.to_string(),
            expected_peer_uid: unexpected_uid,
            expected_peer_gid: files.gid(),
        };

        let err = verify_upstream_peer(&client, &upstream).expect_err("wrong peer rejected");
        assert_eq!(err.kind(), io::ErrorKind::PermissionDenied);
    }
}
