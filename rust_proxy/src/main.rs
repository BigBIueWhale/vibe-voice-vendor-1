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
use std::io::{self, Read, Write};
use std::net::SocketAddr;
use std::panic::AssertUnwindSafe;
use std::path::{Path, PathBuf};
use std::sync::{Arc, RwLock};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
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
use ring::signature::{self, UnparsedPublicKey};
use rustls::pki_types::{pem::PemObject, CertificateDer, PrivateKeyDer};
use rustls::server::WebPkiClientVerifier;
use rustls::RootCertStore;
use serde_json::Value;
use tokio::net::{TcpListener, TcpSocket, TcpStream, UnixStream};
use tokio::signal;
use tokio::sync::{OwnedSemaphorePermit, Semaphore};
use tokio::task::JoinSet;
use tokio_rustls::TlsAcceptor;
use tracing::{debug, error, info, warn, Level};
use x509_parser::pem::Pem;
use x509_parser::prelude::{FromDer, SubjectPublicKeyInfo};
use x509_parser::public_key::PublicKey;

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

    /// Path to ES256 JWT public key PEM file
    #[arg(long)]
    jwt_public_key_file: String,

    /// Path to file listing revoked JWT jti values
    #[arg(long)]
    revoked_tokens_file: String,

    /// Path to TLS certificate PEM file
    #[arg(long)]
    cert_path: String,

    /// Path to TLS private key PEM file
    #[arg(long)]
    key_path: String,

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
// Authentication Limits
// ============================================================================

const REVOCATION_CACHE_TTL: Duration = Duration::from_secs(30);
const MAX_AUTHORIZATION_VALUE_BYTES: usize = 8 * 1024;
const MAX_JWT_DECODED_HEADER_BYTES: usize = 1024;
const MAX_JWT_DECODED_PAYLOAD_BYTES: usize = 4 * 1024;
const MAX_JWT_SUB_BYTES: usize = 256;
const MAX_JWT_JTI_BYTES: usize = 128;
const ES256_SIGNATURE_BYTES: usize = 64;
const MAX_REVOCATION_FILE_BYTES: usize = 1024 * 1024;
const MAX_CONCURRENT_AUTH_VERIFICATIONS: usize = 32;

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

fn auth_error_response(err: AuthError) -> Response<ProxyBody> {
    let (status, body) = match err {
        AuthError::Unauthorized => (StatusCode::UNAUTHORIZED, "Unauthorized"),
        AuthError::Unavailable => (
            StatusCode::SERVICE_UNAVAILABLE,
            "Authentication unavailable",
        ),
        AuthError::Busy => (StatusCode::SERVICE_UNAVAILABLE, "Authentication busy"),
    };
    let mut response = text_response(status, body);
    response
        .headers_mut()
        .insert(header::WWW_AUTHENTICATE, HeaderValue::from_static("Bearer"));
    response
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
    let socket = if addr.is_ipv4() {
        TcpSocket::new_v4()?
    } else {
        TcpSocket::new_v6()?
    };
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

    info!("Generating self-signed certificate");

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

    let hostname_str: String = hostname::get()
        .ok()
        .and_then(|h| h.into_string().ok())
        .unwrap_or_else(|| "localhost".to_string());

    let mut params = CertificateParams::default();
    params
        .distinguished_name
        .push(DnType::CommonName, &hostname_str);
    params.subject_alt_names = vec![
        rcgen::SanType::DnsName(
            hostname_str
                .clone()
                .try_into()
                .map_err(|e| format!("Invalid hostname for SAN: {hostname_str:?}, error: {e}"))?,
        ),
        rcgen::SanType::DnsName(
            "localhost"
                .to_string()
                .try_into()
                .map_err(|e| format!("Invalid 'localhost' SAN (should never happen): {e}"))?,
        ),
        rcgen::SanType::IpAddress(std::net::IpAddr::V4(std::net::Ipv4Addr::LOCALHOST)),
    ];

    let now = time::OffsetDateTime::now_utc();
    params.not_before = now;
    params.not_after = now + time::Duration::days(i64::from(validity_days));

    let key_pair = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256)
        .map_err(|e| format!("Failed to generate ECDSA P-256 key pair: {e}"))?;
    let cert = params
        .self_signed(&key_pair)
        .map_err(|e| format!("Failed to self-sign certificate: {e}"))?;

    std::fs::write(cert_path, cert.pem())
        .map_err(|e| format!("Failed to write cert to {}: {e}", cert_path.display()))?;
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

    info!(
        cn = %hostname_str,
        cert = %cert_path.display(),
        key = %key_path.display(),
        valid_days = validity_days,
        "Self-signed certificate generated"
    );

    Ok(())
}

/// Check whether a PEM certificate file is still valid.
/// Returns `Some(remaining_duration)` if valid, `None` if expired or unreadable.
fn check_cert_expiry(cert_path: &Path) -> Option<Duration> {
    let cert_data = std::fs::read(cert_path).ok()?;
    let pem = Pem::iter_from_buffer(&cert_data).next()?.ok()?;
    let x509 = pem.parse_x509().ok()?;
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
// JWT Authentication
// ============================================================================

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
enum AuthError {
    Unauthorized,
    Unavailable,
    Busy,
}

#[derive(Clone)]
struct AuthVerifier {
    public_key: Arc<Vec<u8>>,
    revoked_tokens_file: Arc<PathBuf>,
    revocation_cache: Arc<RwLock<RevocationCache>>,
    verification_permits: Arc<Semaphore>,
}

struct RevocationCache {
    loaded_at: Instant,
    tokens: HashSet<String>,
}

impl AuthVerifier {
    fn new(
        public_key_file: PathBuf,
        revoked_tokens_file: PathBuf,
    ) -> Result<Self, Box<dyn std::error::Error + Send + Sync>> {
        let public_key = load_es256_public_key(&public_key_file)?;
        let revoked_tokens = load_revoked_tokens(&revoked_tokens_file)?;

        Ok(Self {
            public_key: Arc::new(public_key),
            revoked_tokens_file: Arc::new(revoked_tokens_file),
            revocation_cache: Arc::new(RwLock::new(RevocationCache {
                loaded_at: Instant::now(),
                tokens: revoked_tokens,
            })),
            verification_permits: Arc::new(Semaphore::new(MAX_CONCURRENT_AUTH_VERIFICATIONS)),
        })
    }

    fn verify_headers(&self, headers: &HeaderMap) -> Result<String, AuthError> {
        let token = bearer_token_from_headers(headers)?;
        self.verify_token(token)
    }

    fn verify_token(&self, token: &str) -> Result<String, AuthError> {
        if token.len() > MAX_AUTHORIZATION_VALUE_BYTES {
            return Err(AuthError::Unauthorized);
        }

        let mut parts = token.split('.');
        let encoded_header = parts.next().ok_or(AuthError::Unauthorized)?;
        let encoded_payload = parts.next().ok_or(AuthError::Unauthorized)?;
        let encoded_signature = parts.next().ok_or(AuthError::Unauthorized)?;
        if parts.next().is_some()
            || encoded_header.is_empty()
            || encoded_payload.is_empty()
            || encoded_signature.is_empty()
        {
            return Err(AuthError::Unauthorized);
        }

        let header = decode_json_segment(encoded_header, MAX_JWT_DECODED_HEADER_BYTES)?;
        validate_jwt_header(&header)?;

        let signature = URL_SAFE_NO_PAD
            .decode(encoded_signature.as_bytes())
            .map_err(|_| AuthError::Unauthorized)?;
        if signature.len() != ES256_SIGNATURE_BYTES {
            return Err(AuthError::Unauthorized);
        }

        let payload = decode_json_segment(encoded_payload, MAX_JWT_DECODED_PAYLOAD_BYTES)?;
        validate_temporal_claims(&payload)?;
        let sub = required_string_claim(&payload, "sub", MAX_JWT_SUB_BYTES)?;
        let jti = required_string_claim(&payload, "jti", MAX_JWT_JTI_BYTES)?;

        let signing_input_len = encoded_header.len() + 1 + encoded_payload.len();
        let signing_input = token
            .as_bytes()
            .get(..signing_input_len)
            .ok_or(AuthError::Unauthorized)?;
        let _permit = self
            .verification_permits
            .try_acquire()
            .map_err(|_| AuthError::Busy)?;
        let public_key = UnparsedPublicKey::new(
            &signature::ECDSA_P256_SHA256_FIXED,
            self.public_key.as_slice(),
        );
        public_key
            .verify(signing_input, &signature)
            .map_err(|_| AuthError::Unauthorized)?;

        if self.is_revoked(&jti)? {
            return Err(AuthError::Unauthorized);
        }

        Ok(sub)
    }

    fn is_revoked(&self, jti: &str) -> Result<bool, AuthError> {
        let now = Instant::now();
        {
            let cache = self
                .revocation_cache
                .read()
                .map_err(|_| AuthError::Unavailable)?;
            if now.duration_since(cache.loaded_at) < REVOCATION_CACHE_TTL {
                return Ok(cache.tokens.contains(jti));
            }
        }

        let tokens = load_revoked_tokens(&self.revoked_tokens_file).map_err(|e| {
            error!(error = %e, "Failed to load revoked token list");
            AuthError::Unavailable
        })?;
        let revoked = tokens.contains(jti);
        let mut cache = self
            .revocation_cache
            .write()
            .map_err(|_| AuthError::Unavailable)?;
        *cache = RevocationCache {
            loaded_at: now,
            tokens,
        };
        Ok(revoked)
    }
}

fn bearer_token_from_headers(headers: &HeaderMap) -> Result<&str, AuthError> {
    let mut values = headers.get_all(header::AUTHORIZATION).iter();
    let value = values.next().ok_or(AuthError::Unauthorized)?;
    if values.next().is_some() {
        return Err(AuthError::Unauthorized);
    }

    let value = value.to_str().map_err(|_| AuthError::Unauthorized)?;
    if value.len() > MAX_AUTHORIZATION_VALUE_BYTES {
        return Err(AuthError::Unauthorized);
    }

    let (scheme, token) = value.split_once(' ').ok_or(AuthError::Unauthorized)?;
    if !scheme.eq_ignore_ascii_case("Bearer")
        || token.is_empty()
        || token.bytes().any(|b| b.is_ascii_whitespace())
    {
        return Err(AuthError::Unauthorized);
    }
    Ok(token)
}

fn decode_json_segment(segment: &str, max_decoded_bytes: usize) -> Result<Value, AuthError> {
    let decoded = URL_SAFE_NO_PAD
        .decode(segment.as_bytes())
        .map_err(|_| AuthError::Unauthorized)?;
    if decoded.len() > max_decoded_bytes {
        return Err(AuthError::Unauthorized);
    }
    serde_json::from_slice(&decoded).map_err(|_| AuthError::Unauthorized)
}

fn validate_jwt_header(header: &Value) -> Result<(), AuthError> {
    let header = header.as_object().ok_or(AuthError::Unauthorized)?;
    for key in header.keys() {
        if key != "alg" && key != "typ" {
            return Err(AuthError::Unauthorized);
        }
    }
    if header.get("alg").and_then(Value::as_str) != Some("ES256") {
        return Err(AuthError::Unauthorized);
    }
    if let Some(typ) = header.get("typ") {
        if typ.as_str() != Some("JWT") {
            return Err(AuthError::Unauthorized);
        }
    }
    Ok(())
}

fn validate_temporal_claims(payload: &Value) -> Result<(), AuthError> {
    let payload = payload.as_object().ok_or(AuthError::Unauthorized)?;
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| AuthError::Unavailable)?
        .as_secs();

    if let Some(exp) = payload.get("exp") {
        let exp = numeric_date(exp).ok_or(AuthError::Unauthorized)?;
        if exp <= now {
            return Err(AuthError::Unauthorized);
        }
    }
    if let Some(nbf) = payload.get("nbf") {
        let nbf = numeric_date(nbf).ok_or(AuthError::Unauthorized)?;
        if nbf > now {
            return Err(AuthError::Unauthorized);
        }
    }
    if let Some(iat) = payload.get("iat") {
        numeric_date(iat).ok_or(AuthError::Unauthorized)?;
    }

    Ok(())
}

fn numeric_date(value: &Value) -> Option<u64> {
    value
        .as_u64()
        .or_else(|| value.as_i64().and_then(|v| u64::try_from(v).ok()))
}

fn required_string_claim(
    payload: &Value,
    name: &str,
    max_bytes: usize,
) -> Result<String, AuthError> {
    let claim = payload
        .get(name)
        .and_then(Value::as_str)
        .ok_or(AuthError::Unauthorized)?;
    if claim.is_empty() || claim.len() > max_bytes {
        return Err(AuthError::Unauthorized);
    }
    Ok(claim.to_string())
}

fn load_revoked_tokens(
    path: &Path,
) -> Result<HashSet<String>, Box<dyn std::error::Error + Send + Sync>> {
    let mut file = fs::File::open(path)?;
    let mut text = String::new();
    let bytes_read = Read::by_ref(&mut file)
        .take((MAX_REVOCATION_FILE_BYTES + 1) as u64)
        .read_to_string(&mut text)?;
    if bytes_read > MAX_REVOCATION_FILE_BYTES {
        return Err("revoked token list is too large".into());
    }

    let mut revoked = HashSet::new();
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        if line.len() > MAX_JWT_JTI_BYTES {
            return Err("revoked token jti is too long".into());
        }
        revoked.insert(line.to_string());
    }
    Ok(revoked)
}

fn load_es256_public_key(path: &Path) -> Result<Vec<u8>, Box<dyn std::error::Error + Send + Sync>> {
    let pem = fs::read(path)?;
    for pem in Pem::iter_from_buffer(&pem) {
        let pem = pem.map_err(|e| format!("invalid public key PEM: {e:?}"))?;
        if pem.label != "PUBLIC KEY" {
            continue;
        }

        let (remaining_der, spki) = SubjectPublicKeyInfo::from_der(&pem.contents)
            .map_err(|_| "invalid SubjectPublicKeyInfo DER")?;
        if !remaining_der.is_empty() {
            return Err("trailing data after SubjectPublicKeyInfo".into());
        }
        let key = spki.parsed().map_err(|_| "invalid public key")?;
        let PublicKey::EC(point) = key else {
            return Err("JWT public key must be an EC P-256 key".into());
        };
        let point_data = point.data();
        if point.key_size() != 256 || point_data.len() != 65 || point_data[0] != 4 {
            return Err("JWT public key must be an uncompressed P-256 point".into());
        }
        return Ok(point_data.to_vec());
    }

    Err("public key PEM does not contain a PUBLIC KEY block".into())
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
    auth_verifier: AuthVerifier,
}

impl AppState {
    fn new(upstream: UpstreamTarget, max_body_size: usize, auth_verifier: AuthVerifier) -> Self {
        Self {
            upstream,
            max_body_size,
            auth_verifier,
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
    req: Request<B>,
) -> Response<ProxyBody>
where
    B: HttpBody<Data = Bytes> + Send + 'static,
    B::Error: Into<BoxError>,
{
    let result = AssertUnwindSafe(proxy_handler_inner(state, client_addr, req))
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
    req: Request<B>,
) -> Response<ProxyBody>
where
    B: HttpBody<Data = Bytes> + Send + 'static,
    B::Error: Into<BoxError>,
{
    let path = req.uri().path().to_string();
    if let Err(err) = state.auth_verifier.verify_headers(req.headers()) {
        debug!(
            client = %client_addr,
            path = %path,
            error = ?err,
            "Rejected request before proxying"
        );
        return auth_error_response(err);
    }

    if path == "/health" {
        return local_health_response(req.method());
    }

    if is_http_upgrade(req.headers()) {
        return text_response(StatusCode::NOT_FOUND, "Not Found");
    }

    http_proxy(state, req, client_addr).await
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
        if !is_hop_by_hop_header(key, &request_connection_tokens) {
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

    let service = service_fn(move |req: Request<Incoming>| {
        let state = state.clone();
        async move { Ok::<_, Infallible>(proxy_handler(state, client_addr, req).await) }
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
        client_ca_cert_path.clone(),
        args.cert_validity_days,
        args.cert_check_interval_secs,
        tls_config.clone(),
    ));

    let auth_verifier = AuthVerifier::new(
        PathBuf::from(&args.jwt_public_key_file),
        PathBuf::from(&args.revoked_tokens_file),
    )
    .unwrap_or_else(|e| panic!("Failed to initialize JWT verifier: {e}"));

    let upstream = UpstreamTarget::from_args(&args);

    let state = AppState::new(upstream.clone(), args.max_body_size, auth_verifier);

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
    use http_body_util::{Empty, Full};
    use std::os::unix::fs::{MetadataExt, PermissionsExt};
    use std::pin::Pin;
    use std::process;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::task::{Context, Poll};

    const TEST_PUBLIC_PEM: &str = "-----BEGIN PUBLIC KEY-----\n\
MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE6Qzrhx04jK357AQGxktuXWYDXuFc\n\
0XHE9I3d0nYGXC605q/IJjBb6naEi5dTT+CxyA2Deba+TLWggp0R/cq/DA==\n\
-----END PUBLIC KEY-----\n";

    const TEST_TOKEN: &str = "\
eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCJ9.\
eyJzdWIiOiJwcm94eS10ZXN0LXVzZXIiLCJqdGkiOiJwcm94eS10ZXN0LWp0aSJ9.\
XJuGmMHelpKPeEZCG-COaRzek0HSlriS5Yq_doPG497iGZCJEkm38a9QHE1iwZuVXDTs-_7Cjnfu2MfyRzwuow";

    const TEST_REVOKED_TOKEN: &str = "\
eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCJ9.\
eyJzdWIiOiJwcm94eS10ZXN0LXVzZXIiLCJqdGkiOiJyZXZva2VkLWp0aSJ9.\
gtqOUBjrEaX62UGoppp76hGVsRevQ7i5niX-PZK1oghIZdcp9yIasw7hN3xaTMhTOyNktiGdY-bh3W670mWj9g";

    struct TestFiles {
        dir: PathBuf,
        public_key: PathBuf,
        revoked_tokens: PathBuf,
    }

    impl TestFiles {
        fn new(revoked: &str) -> Self {
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
            let public_key = dir.join("public.pem");
            let revoked_tokens = dir.join("revoked.txt");
            fs::write(&public_key, TEST_PUBLIC_PEM).expect("write public key");
            fs::write(&revoked_tokens, revoked).expect("write revoked token file");
            Self {
                dir,
                public_key,
                revoked_tokens,
            }
        }

        fn verifier(&self) -> AuthVerifier {
            AuthVerifier::new(self.public_key.clone(), self.revoked_tokens.clone())
                .expect("auth verifier")
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
                self.verifier(),
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

    fn request_body(body: &'static [u8]) -> Full<Bytes> {
        Full::new(Bytes::from_static(body))
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

    #[cfg(unix)]
    #[test]
    fn generated_tls_private_key_is_private_at_rest() {
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

        generate_self_signed_cert(&cert_path, &key_path, 1).expect("generate cert");

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

    #[test]
    fn proxy_auth_accepts_valid_es256_token() {
        let files = TestFiles::new("");
        assert_eq!(
            files
                .verifier()
                .verify_token(TEST_TOKEN)
                .expect("valid token"),
            "proxy-test-user"
        );
    }

    #[test]
    fn proxy_auth_rejects_revoked_token() {
        let files = TestFiles::new("revoked-jti\n");
        assert!(matches!(
            files.verifier().verify_token(TEST_REVOKED_TOKEN),
            Err(AuthError::Unauthorized)
        ));
    }

    #[test]
    fn proxy_auth_rejects_oversized_revocation_file() {
        let revoked = "x".repeat(MAX_REVOCATION_FILE_BYTES + 1);
        let files = TestFiles::new(&revoked);
        assert!(AuthVerifier::new(files.public_key.clone(), files.revoked_tokens.clone()).is_err());
    }

    #[test]
    fn proxy_auth_rejects_overlong_revocation_entry() {
        let revoked = format!("{}\n", "j".repeat(MAX_JWT_JTI_BYTES + 1));
        let files = TestFiles::new(&revoked);
        assert!(AuthVerifier::new(files.public_key.clone(), files.revoked_tokens.clone()).is_err());
    }

    #[test]
    fn proxy_auth_sheds_when_verifier_concurrency_is_exhausted() {
        let files = TestFiles::new("");
        let verifier = files.verifier();
        let _permits: Vec<_> = (0..MAX_CONCURRENT_AUTH_VERIFICATIONS)
            .map(|_| {
                verifier
                    .verification_permits
                    .try_acquire()
                    .expect("permit available")
            })
            .collect();

        assert!(matches!(
            verifier.verify_token(TEST_TOKEN),
            Err(AuthError::Busy)
        ));
    }

    #[test]
    fn proxy_auth_rejects_missing_invalid_and_duplicate_headers() {
        let files = TestFiles::new("");
        let verifier = files.verifier();

        let headers = HeaderMap::new();
        assert!(matches!(
            verifier.verify_headers(&headers),
            Err(AuthError::Unauthorized)
        ));

        let mut headers = HeaderMap::new();
        headers.insert(
            header::AUTHORIZATION,
            HeaderValue::from_static("Bearer not-a-jwt"),
        );
        assert!(matches!(
            verifier.verify_headers(&headers),
            Err(AuthError::Unauthorized)
        ));

        let mut headers = HeaderMap::new();
        headers.append(
            header::AUTHORIZATION,
            HeaderValue::from_str(&format!("Bearer {TEST_TOKEN}")).expect("header value"),
        );
        headers.append(
            header::AUTHORIZATION,
            HeaderValue::from_static("Bearer not-a-jwt"),
        );
        assert!(matches!(
            verifier.verify_headers(&headers),
            Err(AuthError::Unauthorized)
        ));
    }

    #[tokio::test]
    async fn proxy_health_requires_auth_and_stays_local() {
        let files = TestFiles::new("");
        let unauth_body_polls = Arc::new(AtomicUsize::new(0));
        let unauth_req = Request::builder()
            .method(Method::GET)
            .uri("/health")
            .body(body_that_counts_polls(unauth_body_polls.clone()))
            .expect("request");

        let unauth_response = proxy_handler_inner(
            files.state(),
            SocketAddr::from(([127, 0, 0, 1], 12345)),
            unauth_req,
        )
        .await;

        assert_eq!(unauth_response.status(), StatusCode::UNAUTHORIZED);
        assert_eq!(unauth_body_polls.load(Ordering::SeqCst), 0);

        let req = Request::builder()
            .method(Method::GET)
            .uri("/health")
            .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
            .body(empty_request_body())
            .expect("request");

        let response = proxy_handler_inner(
            files.state(),
            SocketAddr::from(([127, 0, 0, 1], 12345)),
            req,
        )
        .await;

        assert_eq!(response.status(), StatusCode::OK);
        let body = response_body_bytes(response, 1024).await;
        assert_eq!(&body[..], br#"{"status":"ok","proxy":"ok"}"#);
    }

    #[tokio::test]
    async fn proxy_rejects_unauthorized_request_before_upstream() {
        let files = TestFiles::new("");
        let req = Request::builder()
            .method(Method::POST)
            .uri("/v1/transcribe")
            .body(request_body(b"body must not be proxied"))
            .expect("request");

        let response = proxy_handler_inner(
            files.state(),
            SocketAddr::from(([127, 0, 0, 1], 12345)),
            req,
        )
        .await;

        assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
    }

    #[tokio::test]
    async fn proxy_rejects_unauthorized_request_without_polling_body() {
        let files = TestFiles::new("");
        let body_polls = Arc::new(AtomicUsize::new(0));
        let req = Request::builder()
            .method(Method::POST)
            .uri("/v1/transcribe")
            .header(header::AUTHORIZATION, "Bearer not-a-valid-jwt")
            .body(body_that_counts_polls(body_polls.clone()))
            .expect("request");

        let response = proxy_handler_inner(
            files.state(),
            SocketAddr::from(([127, 0, 0, 1], 12345)),
            req,
        )
        .await;

        assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
        assert_eq!(body_polls.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn proxy_does_not_send_continue_for_unauthorized_expect_request() {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};

        let files = TestFiles::new("");
        let state = files.state();
        let (mut client_io, server_io) = tokio::io::duplex(4096);
        let service = service_fn(move |req: Request<Incoming>| {
            let state = state.clone();
            async move {
                Ok::<_, Infallible>(
                    proxy_handler(state, SocketAddr::from(([127, 0, 0, 1], 12345)), req).await,
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
                  Authorization: Bearer not-a-valid-jwt\r\n\
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
        assert!(response.starts_with("HTTP/1.1 401 Unauthorized"));
        assert!(!response.contains("100 Continue"));

        server
            .await
            .expect("server task")
            .expect("HTTP/1 connection");
    }

    #[tokio::test]
    async fn proxy_auth_runs_before_oversized_body_check() {
        let files = TestFiles::new("");
        let req = Request::builder()
            .method(Method::POST)
            .uri("/v1/transcribe")
            .header(header::CONTENT_LENGTH, "999999")
            .body(empty_request_body())
            .expect("request");

        let response = proxy_handler_inner(
            files.state(),
            SocketAddr::from(([127, 0, 0, 1], 12345)),
            req,
        )
        .await;

        assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
    }

    #[tokio::test]
    async fn proxy_rejects_authenticated_oversized_content_length() {
        let files = TestFiles::new("");
        let req = Request::builder()
            .method(Method::POST)
            .uri("/v1/transcribe")
            .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
            .header(header::CONTENT_LENGTH, "1025")
            .body(empty_request_body())
            .expect("request");

        let response = proxy_handler_inner(
            files.state(),
            SocketAddr::from(([127, 0, 0, 1], 12345)),
            req,
        )
        .await;

        assert_eq!(response.status(), StatusCode::PAYLOAD_TOO_LARGE);
    }

    #[tokio::test]
    async fn proxy_rejects_authenticated_http_upgrade_without_upstream() {
        let files = TestFiles::new("");
        let body_polls = Arc::new(AtomicUsize::new(0));
        let req = Request::builder()
            .method(Method::POST)
            .uri("/v1/transcribe")
            .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
            .header(header::UPGRADE, "h2c")
            .body(body_that_counts_polls(body_polls.clone()))
            .expect("request");

        let response = proxy_handler_inner(
            files.state(),
            SocketAddr::from(([127, 0, 0, 1], 12345)),
            req,
        )
        .await;

        assert_eq!(response.status(), StatusCode::NOT_FOUND);
        assert_eq!(body_polls.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn proxy_requires_auth_before_public_route_allowlist() {
        let files = TestFiles::new("");
        let req = Request::builder()
            .method(Method::GET)
            .uri("/not-a-public-route")
            .body(empty_request_body())
            .expect("request");

        let response = proxy_handler_inner(
            files.state(),
            SocketAddr::from(([127, 0, 0, 1], 12345)),
            req,
        )
        .await;

        assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
    }

    #[tokio::test]
    async fn proxy_rejects_authenticated_unknown_route_without_upstream() {
        let files = TestFiles::new("");
        let body_polls = Arc::new(AtomicUsize::new(0));
        let req = Request::builder()
            .method(Method::POST)
            .uri("/v1/not-real")
            .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
            .body(body_that_counts_polls(body_polls.clone()))
            .expect("request");

        let response = proxy_handler_inner(
            files.state(),
            SocketAddr::from(([127, 0, 0, 1], 12345)),
            req,
        )
        .await;

        assert_eq!(response.status(), StatusCode::NOT_FOUND);
        assert_eq!(body_polls.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn proxy_rejects_authenticated_wrong_method_without_upstream() {
        let files = TestFiles::new("");
        let req = Request::builder()
            .method(Method::GET)
            .uri("/v1/transcribe")
            .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
            .body(empty_request_body())
            .expect("request");

        let response = proxy_handler_inner(
            files.state(),
            SocketAddr::from(([127, 0, 0, 1], 12345)),
            req,
        )
        .await;

        assert_eq!(response.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn proxy_forwards_authenticated_http_over_unix_socket() {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        use tokio::net::UnixListener;

        let files = TestFiles::new("");
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
            assert!(!request_lower.contains("x-strip-request:"));
            assert!(request_lower.contains("x-keep-request: visible"));
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
            files.verifier(),
        );
        let req = Request::builder()
            .method(Method::GET)
            .uri("/v1/queue/status")
            .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
            .header(header::CONNECTION, "keep-alive, x-strip-request")
            .header("x-strip-request", "secret")
            .header("x-keep-request", "visible")
            .body(empty_request_body())
            .expect("request");

        let response =
            proxy_handler_inner(state, SocketAddr::from(([127, 0, 0, 1], 12345)), req).await;
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

        let files = TestFiles::new("");
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
        let files = TestFiles::new("");
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
