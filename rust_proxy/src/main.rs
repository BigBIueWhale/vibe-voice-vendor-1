//! VibeVoice TLS Reverse Proxy
//!
//! Self-contained, zero-install SSL-terminating reverse proxy for the
//! VibeVoice ASR server. Generates self-signed certificates automatically
//! and hot-reloads them on expiry with zero downtime.
//!
//! Architecture:
//!     Internet --> vvv_proxy :7853 (HTTPS, 0.0.0.0) --> 127.0.0.1:8080 (FastAPI)

use std::net::SocketAddr;
use std::panic::AssertUnwindSafe;
use std::path::{Path, PathBuf};
use std::time::Duration;

use axum::body::Body;
use axum::extract::ws::{
    CloseFrame as AxumCloseFrame, Message as AxumMessage, WebSocket, WebSocketUpgrade,
};
use axum::extract::{ConnectInfo, FromRequest, Request, State};
use axum::http::{header, HeaderMap, HeaderName, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::Router;
use axum_server::Handle;
use futures::future::FutureExt;
use futures::stream::StreamExt;
use futures::SinkExt;
use http_body_util::BodyExt;
use tokio::signal;
use tokio_tungstenite::tungstenite::{
    self,
    client::IntoClientRequest,
    protocol::CloseFrame as TungsteniteCloseFrame,
    Message as TungsteniteMessage,
};
use tower_http::limit::RequestBodyLimitLayer;
use tracing::{debug, error, info, warn, Level};
use x509_parser::pem::Pem;

// ============================================================================
// Hard-Coded Configuration
// ============================================================================

/// Upstream FastAPI server address. Always localhost, never exposed to the network.
const UPSTREAM_HOST: &str = "127.0.0.1";
const UPSTREAM_PORT: u16 = 8080;

/// HTTPS listener. Binds to all interfaces on a fixed port.
const HTTPS_BIND: [u8; 4] = [0, 0, 0, 0];
const HTTPS_PORT: u16 = 7853;

/// Request body limit in bytes: 500 MB (matches server's VVV_MAX_AUDIO_BYTES default).
const MAX_BODY_SIZE: usize = 500 * 1024 * 1024;

/// Self-signed certificate paths (relative to the working directory).
const CERT_PATH: &str = "certs/self-signed/fullchain.pem";
const KEY_PATH: &str = "certs/self-signed/privkey.pem";

/// Certificate validity: 10 years.
const CERT_VALIDITY_DAYS: u32 = 3650;

/// How often to check certificate expiry (1 hour — generous for a 10-year cert).
const CERT_CHECK_INTERVAL_SECS: u64 = 3600;

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

/// WebSocket headers forwarded to upstream via allowlist.
/// Excludes sec-websocket-extensions to prevent permessage-deflate mismatches
/// (upstream would negotiate compression but tungstenite would not decompress,
/// causing "Reserved bits are non-zero" errors when RSV1 is set).
const WEBSOCKET_FORWARD_HEADERS: &[&str] = &[
    "sec-websocket-protocol",
    "origin",
    "cookie",
    "authorization",
];

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

// ============================================================================
// Self-Signed Certificate Management
// ============================================================================

/// Generate a self-signed ECDSA P-256 certificate and write it to disk.
/// Creates parent directories if they do not exist.
fn generate_self_signed_cert(
    cert_path: &Path,
    key_path: &Path,
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
    }

    let hostname_str: String = hostname::get()
        .ok()
        .and_then(|h| h.into_string().ok())
        .unwrap_or_else(|| "localhost".to_string());

    let mut params = CertificateParams::default();
    params.distinguished_name.push(DnType::CommonName, &hostname_str);
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
    params.not_after = now + time::Duration::days(i64::from(CERT_VALIDITY_DAYS));

    let key_pair = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256)
        .map_err(|e| format!("Failed to generate ECDSA P-256 key pair: {e}"))?;
    let cert = params
        .self_signed(&key_pair)
        .map_err(|e| format!("Failed to self-sign certificate: {e}"))?;

    std::fs::write(cert_path, cert.pem())
        .map_err(|e| format!("Failed to write cert to {}: {e}", cert_path.display()))?;
    std::fs::write(key_path, key_pair.serialize_pem())
        .map_err(|e| format!("Failed to write key to {}: {e}", key_path.display()))?;

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(key_path, std::fs::Permissions::from_mode(0o600))
            .map_err(|e| format!("Failed to restrict key permissions on {}: {e}", key_path.display()))?;
    }

    info!(
        cn = %hostname_str,
        cert = %cert_path.display(),
        key = %key_path.display(),
        valid_days = CERT_VALIDITY_DAYS,
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
    Some(Duration::new(secs as u64, remaining.subsec_nanoseconds() as u32))
}

/// Background task: periodically check certificate expiry and hot-reload if expired.
async fn cert_renewal_task(
    cert_path: PathBuf,
    key_path: PathBuf,
    tls_config: axum_server::tls_rustls::RustlsConfig,
) {
    let interval = Duration::from_secs(CERT_CHECK_INTERVAL_SECS);
    loop {
        tokio::time::sleep(interval).await;

        if check_cert_expiry(&cert_path).is_some() {
            continue;
        }

        warn!("Certificate expired or unreadable — regenerating");
        if let Err(e) = generate_self_signed_cert(&cert_path, &key_path) {
            error!(error = %e, "Certificate regeneration failed");
            continue;
        }
        match tls_config.reload_from_pem_file(&cert_path, &key_path).await {
            Ok(()) => info!("Certificate hot-reloaded successfully (zero downtime)"),
            Err(e) => error!(error = %e, "Certificate hot-reload failed"),
        }
    }
}

// ============================================================================
// Application State
// ============================================================================

#[derive(Clone)]
struct AppState {
    upstream_url: String,
    http_client: reqwest::Client,
}

impl AppState {
    fn new() -> Self {
        let http_client = reqwest::Client::builder()
            .timeout(Duration::from_secs(600))
            .connect_timeout(Duration::from_secs(10))
            .pool_max_idle_per_host(50)
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .expect("Failed to build HTTP client — TLS backend unavailable");

        Self {
            upstream_url: format!("http://{UPSTREAM_HOST}:{UPSTREAM_PORT}"),
            http_client,
        }
    }
}

// ============================================================================
// WebSocket Message Conversion
// ============================================================================

/// Convert an axum WebSocket message to a tungstenite message.
/// Required because axum and tungstenite define structurally identical but
/// distinct message types.
fn axum_to_tungstenite(msg: AxumMessage) -> TungsteniteMessage {
    match msg {
        AxumMessage::Text(text) => TungsteniteMessage::Text(text.as_str().to_string().into()),
        AxumMessage::Binary(data) => TungsteniteMessage::Binary(data.to_vec().into()),
        AxumMessage::Ping(data) => TungsteniteMessage::Ping(data.to_vec().into()),
        AxumMessage::Pong(data) => TungsteniteMessage::Pong(data.to_vec().into()),
        AxumMessage::Close(frame) => TungsteniteMessage::Close(frame.map(|f| TungsteniteCloseFrame {
            code: tungstenite::protocol::frame::coding::CloseCode::from(f.code),
            reason: f.reason.to_string().into(),
        })),
    }
}

/// Convert a tungstenite message to an axum WebSocket message.
/// Returns `None` for internal `Frame` variants that have no axum equivalent.
fn tungstenite_to_axum(msg: TungsteniteMessage) -> Option<AxumMessage> {
    match msg {
        TungsteniteMessage::Text(text) => Some(AxumMessage::Text(text.as_str().to_string().into())),
        TungsteniteMessage::Binary(data) => Some(AxumMessage::Binary(data.to_vec().into())),
        TungsteniteMessage::Ping(data) => Some(AxumMessage::Ping(data.to_vec().into())),
        TungsteniteMessage::Pong(data) => Some(AxumMessage::Pong(data.to_vec().into())),
        TungsteniteMessage::Close(frame) => Some(AxumMessage::Close(frame.map(|f| AxumCloseFrame {
            code: f.code.into(),
            reason: f.reason.to_string().into(),
        }))),
        TungsteniteMessage::Frame(_) => None,
    }
}

// ============================================================================
// Proxy Handlers
// ============================================================================

/// Top-level request handler. Detects WebSocket upgrades and dispatches
/// to the appropriate proxy path. Wrapped in `catch_unwind` so that a
/// panic in any single request does not crash the entire server.
#[axum::debug_handler]
async fn proxy_handler(
    State(state): State<AppState>,
    ConnectInfo(client_addr): ConnectInfo<SocketAddr>,
    req: Request,
) -> Response {
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
            (StatusCode::INTERNAL_SERVER_ERROR, "Internal server error").into_response()
        }
    }
}

async fn proxy_handler_inner(
    state: AppState,
    client_addr: SocketAddr,
    req: Request,
) -> Response {
    let is_websocket = req
        .headers()
        .get(header::UPGRADE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.eq_ignore_ascii_case("websocket"));

    if is_websocket {
        let (parts, body) = req.into_parts();
        let path = parts
            .uri
            .path_and_query()
            .map(|pq| pq.to_string())
            .unwrap_or_default();
        let headers = parts.headers.clone();
        let req = Request::from_parts(parts, body);

        match WebSocketUpgrade::from_request(req, &state).await {
            Ok(ws) => {
                let protocols = extract_ws_protocols(&headers);
                return ws.protocols(protocols).on_upgrade(move |socket| {
                    websocket_proxy(socket, state, path, headers, client_addr)
                });
            }
            Err(rejection) => {
                error!(error = ?rejection, "WebSocket upgrade rejected");
                return rejection.into_response();
            }
        }
    }

    http_proxy(state, req, client_addr).await
}

/// Extract `Sec-WebSocket-Protocol` subprotocols from request headers.
fn extract_ws_protocols(headers: &HeaderMap) -> Vec<String> {
    headers
        .get("sec-websocket-protocol")
        .and_then(|v| v.to_str().ok())
        .map(|s| s.split(',').map(|p| p.trim().to_string()).collect())
        .unwrap_or_default()
}

/// Reverse-proxy an HTTP request to the upstream FastAPI server.
/// Streams the response body back without buffering (critical for SSE).
async fn http_proxy(state: AppState, req: Request, client_addr: SocketAddr) -> Response {
    let method = req.method().clone();
    let uri = req.uri().clone();
    let path_query = uri.path_and_query().map(|pq| pq.as_str()).unwrap_or("/");
    let target_url = format!("{}{}", state.upstream_url, path_query);

    debug!(method = %method, path = %path_query, client = %client_addr, "Proxying HTTP");

    // Build upstream headers: strip hop-by-hop, inject forwarding metadata.
    let mut upstream_headers = HeaderMap::new();
    for (key, value) in req.headers() {
        if !HOP_BY_HOP_HEADERS.contains(&key.as_str().to_lowercase().as_str()) {
            upstream_headers.insert(key.clone(), value.clone());
        }
    }
    if let Ok(host_val) = HeaderValue::from_str(&format!("{UPSTREAM_HOST}:{UPSTREAM_PORT}")) {
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

    let body_bytes = match req.into_body().collect().await {
        Ok(collected) => collected.to_bytes(),
        Err(e) => {
            error!(error = %e, "Failed to read request body");
            return (StatusCode::BAD_REQUEST, "Failed to read request body").into_response();
        }
    };

    let upstream_response = match state
        .http_client
        .request(method, &target_url)
        .headers(upstream_headers)
        .body(body_bytes)
        .send()
        .await
    {
        Ok(resp) => resp,
        Err(e) => {
            error!(
                target = %target_url,
                client = %client_addr,
                error = %e,
                "Upstream request failed"
            );
            return (StatusCode::BAD_GATEWAY, "Bad Gateway").into_response();
        }
    };

    // Build the client-facing response.
    let status = upstream_response.status();
    let mut response_headers = HeaderMap::new();

    for (name, value) in security_headers() {
        response_headers.insert(name, value);
    }

    // Copy upstream headers. Strip hop-by-hop, content-length (axum recalculates
    // for streamed bodies), and server (privacy). Use append() to preserve
    // multiple Set-Cookie headers.
    for (key, value) in upstream_response.headers() {
        let lower = key.as_str().to_lowercase();
        if !HOP_BY_HOP_HEADERS.contains(&lower.as_str())
            && lower != "content-length"
            && lower != "server"
        {
            response_headers.append(key.clone(), value.clone());
        }
    }

    // Stream the body without buffering — critical for SSE (flush_interval -1).
    let body = Body::from_stream(upstream_response.bytes_stream());
    let mut response = Response::new(body);
    *response.status_mut() = status;
    *response.headers_mut() = response_headers;
    response
}

/// Bidirectionally proxy a WebSocket connection to the upstream server.
async fn websocket_proxy(
    client_socket: WebSocket,
    state: AppState,
    path: String,
    headers: HeaderMap,
    client_addr: SocketAddr,
) {
    let ws_url = format!(
        "ws://{}{}",
        state.upstream_url.trim_start_matches("http://"),
        path
    );

    debug!(upstream = %ws_url, client = %client_addr, "Opening WebSocket proxy");

    let mut request = match ws_url.clone().into_client_request() {
        Ok(req) => req,
        Err(e) => {
            error!(error = %e, url = %ws_url, "Failed to build upstream WebSocket request");
            return;
        }
    };

    // Forward only allowlisted headers to prevent protocol errors.
    for header_name in WEBSOCKET_FORWARD_HEADERS {
        if let Some(value) = headers.get(*header_name) {
            if let Ok(name) = tungstenite::http::HeaderName::try_from(*header_name) {
                if let Ok(val) = tungstenite::http::HeaderValue::from_bytes(value.as_bytes()) {
                    request.headers_mut().insert(name, val);
                }
            }
        }
    }

    let upstream_socket = match tokio_tungstenite::connect_async(request).await {
        Ok((socket, resp)) => {
            debug!(status = %resp.status(), "WebSocket upstream connected");
            socket
        }
        Err(e) => {
            error!(upstream = %ws_url, error = %e, "WebSocket upstream connection failed");
            return;
        }
    };

    let (mut client_tx, mut client_rx) = client_socket.split();
    let (mut upstream_tx, mut upstream_rx) = upstream_socket.split();

    let client_to_upstream = async {
        while let Some(Ok(msg)) = client_rx.next().await {
            if upstream_tx.send(axum_to_tungstenite(msg)).await.is_err() {
                break;
            }
        }
        let _ = upstream_tx.close().await;
    };

    let upstream_to_client = async {
        while let Some(Ok(msg)) = upstream_rx.next().await {
            if let Some(axum_msg) = tungstenite_to_axum(msg) {
                if client_tx.send(axum_msg).await.is_err() {
                    break;
                }
            }
        }
        let _ = client_tx.close().await;
    };

    // Run both directions concurrently; when one side closes, the other follows.
    tokio::select! {
        _ = client_to_upstream => {}
        _ = upstream_to_client => {}
    }

    debug!(client = %client_addr, "WebSocket proxy closed");
}

// ============================================================================
// Server Setup
// ============================================================================

fn build_router() -> Router {
    let state = AppState::new();
    Router::new()
        .route("/{*path}", any(proxy_handler))
        .route("/", any(proxy_handler))
        .layer(RequestBodyLimitLayer::new(MAX_BODY_SIZE))
        .with_state(state)
}

/// Wait for Ctrl+C or SIGTERM, then trigger a graceful 10-second drain.
async fn shutdown_signal(handle: Handle) {
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
    handle.graceful_shutdown(Some(Duration::from_secs(10)));
}

// ============================================================================
// Entry Point
// ============================================================================

#[tokio::main]
async fn main() {
    rustls::crypto::ring::default_provider()
        .install_default()
        .expect("Failed to install rustls ring crypto provider");

    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::from_default_env()
                .add_directive(Level::INFO.into()),
        )
        .init();

    let cert_path = PathBuf::from(CERT_PATH);
    let key_path = PathBuf::from(KEY_PATH);

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
            generate_self_signed_cert(&cert_path, &key_path)
                .unwrap_or_else(|e| panic!("Failed to generate initial certificate: {e}"));
        }
    }

    let tls_config =
        axum_server::tls_rustls::RustlsConfig::from_pem_file(&cert_path, &key_path)
            .await
            .unwrap_or_else(|e| {
                panic!(
                    "Failed to load TLS config from cert={}, key={}: {e}",
                    cert_path.display(),
                    key_path.display()
                )
            });

    // Background task: regenerate and hot-reload the cert if it ever expires.
    tokio::spawn(cert_renewal_task(
        cert_path.clone(),
        key_path.clone(),
        tls_config.clone(),
    ));

    let app = build_router();
    let addr = SocketAddr::from((HTTPS_BIND, HTTPS_PORT));

    let handle = Handle::new();
    tokio::spawn(shutdown_signal(handle.clone()));

    info!("VibeVoice TLS Proxy ready");
    info!("  HTTPS: https://0.0.0.0:{HTTPS_PORT}");
    info!("  Upstream: http://{UPSTREAM_HOST}:{UPSTREAM_PORT}");

    axum_server::bind_rustls(addr, tls_config)
        .handle(handle)
        .serve(app.into_make_service_with_connect_info::<SocketAddr>())
        .await
        .unwrap_or_else(|e| panic!("HTTPS server failed on {addr}: {e}"));

    info!("Reverse proxy stopped");
}
