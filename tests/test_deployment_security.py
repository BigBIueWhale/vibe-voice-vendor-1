import shlex
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text()


def _docker_run_commands(script: str) -> list[str]:
    commands: list[str] = []
    lines = script.splitlines()
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if "docker run " not in stripped or stripped.startswith("echo "):
            index += 1
            continue

        start = stripped.index("docker run ")
        parts = [stripped[start:].rstrip("\\").strip()]
        while lines[index].rstrip().endswith("\\"):
            index += 1
            parts.append(lines[index].strip().rstrip("\\").strip())
        command = " ".join(parts)
        command = command.removesuffix('2>&1)"; then').strip()
        commands.append(command)
        index += 1
    return commands


def test_local_backend_containers_do_not_use_host_network_or_published_ports() -> None:
    setup = _read("setup.sh")
    dockerfile = _read("Dockerfile")

    forbidden = [
        "--network host",
        "--net=host",
        "--ipc=host",
        "--publish",
        "-p 127",
        "-p 0.0.0.0",
        "nvidia/cuda:",
        "GPU_SMOKE_IMAGE",
        "host.docker.internal",
        "--add-host",
    ]
    for value in forbidden:
        assert value not in setup
        assert value not in dockerfile

    assert "--network none" in setup
    assert '--network "container:$BACKEND_NETNS_CONTAINER"' in setup
    assert "--ipc=private" in setup
    assert "--pull=never" in _read("README.md")
    assert "docker run --rm --pull=never --gpus all" in setup
    assert "Verifying GPU passthrough inside pinned $VLLM_IMAGE with no Docker network" in setup
    assert "torch.cuda.is_available()" in setup
    assert setup.count("--pull=never") >= 4


def test_all_docker_runs_fail_closed_and_do_not_join_host_namespaces() -> None:
    commands = _docker_run_commands(_read("setup.sh"))
    assert len(commands) == 4

    forbidden_tokens = [
        "--privileged",
        "--pid=host",
        "--cgroupns=host",
        "--uts=host",
        "--userns=host",
        "--network host",
        "--net=host",
        "--ipc=host",
        "--cap-add",
        "--publish",
        "-p",
        "host.docker.internal",
        "--add-host",
    ]
    forbidden_substrings = [
        "/var/run/docker.sock",
        "/run/docker.sock",
    ]
    for command in commands:
        tokens = shlex.split(command)
        assert "--pull=never" in command
        assert "--cap-drop ALL" in command
        assert "--security-opt no-new-privileges:true" in command
        assert "--pids-limit" in command
        for value in forbidden_tokens:
            assert value not in tokens
        for value in forbidden_substrings:
            assert value not in command
        assert not any(token.startswith("-p") and token != "--pids-limit" for token in tokens)

    assert any(
        "--rm" in command
        and "--network none" in command
        and "torch.cuda.is_available()" in command
        for command in commands
    )
    assert any(
        '--name "$BACKEND_NETNS_CONTAINER"' in command and "--network none" in command
        for command in commands
    )
    assert any(
        '--name "$VLLM_CONTAINER"' in command
        and '--network "container:$BACKEND_NETNS_CONTAINER"' in command
        for command in commands
    )
    assert any(
        '--name "$SERVER_CONTAINER"' in command
        and '--network "container:$BACKEND_NETNS_CONTAINER"' in command
        and '-v "$BACKEND_SOCKET_DIR:/run/vibevoice"' in command
        for command in commands
    )


def test_local_backend_host_access_is_only_the_private_uds_mount() -> None:
    setup = _read("setup.sh")

    assert '-v "$BACKEND_SOCKET_DIR:/run/vibevoice"' in setup
    assert '--uds "$BACKEND_SOCKET_CONTAINER"' in setup
    assert "--vllm-base-url http://127.0.0.1:8000" in setup
    assert "ensure_private_dir" in setup
    assert 'ensure_private_dir "$APP_CONFIG_DIR"' in setup
    assert 'ensure_private_dir "$BACKEND_SOCKET_DIR"' in setup
    assert 'stat -c \'%u:%g\' "$dir"' in setup
    assert 'stat -c \'%a\' "$dir"' in setup
    assert "backend_socket_is_private" in setup
    assert '[[ -S "$BACKEND_SOCKET_HOST" && ! -L "$BACKEND_SOCKET_HOST" ]]' in setup
    assert 'stat -c \'%u:%g\' "$BACKEND_SOCKET_HOST"' in setup
    assert 'stat -c \'%a\' "$BACKEND_SOCKET_HOST"' in setup

    forbidden_mounts = [
        "keys/private.pem:/",
        "$INSTALL_DIR:/",
        "$HOME:/",
        "/:/",
        "/var/run/docker.sock",
        "/run/docker.sock",
        "/proc:/",
        "/sys:/",
    ]
    for value in forbidden_mounts:
        assert value not in setup


def test_vllm_runtime_has_no_local_media_path_and_loopback_only_bind() -> None:
    dockerfile = _read("Dockerfile")
    setup = _read("setup.sh")

    assert "--allowed-local-media-path" not in dockerfile
    assert "--allowed-local-media-path /tmp" not in _read("README.md")
    assert "--allowed-local-media-path" not in _read("doc/vibevoice-asr-quality-investigation.md")
    assert '[[ "$image_cmd" != *"--allowed-local-media-path"* ]]' in setup
    assert '"--host", "127.0.0.1"' in dockerfile
    assert '"--port", "8000"' in dockerfile


def test_vllm_image_reuse_is_source_fingerprinted() -> None:
    setup = _read("setup.sh")

    assert "VLLM_IMAGE_SECURITY_PROFILE=" in setup
    assert "compute_vllm_image_source_hash" in setup
    assert "vllm_image_matches_current_sources" in setup
    assert 'sha256sum Dockerfile pyproject.toml uv.lock' in setup
    assert "find server -type f -print0" in setup
    assert "find VibeVoice -type f" in setup
    assert "! -path 'VibeVoice/.git/*'" in setup
    assert "! -path 'VibeVoice/demo/*'" in setup
    assert "org.vvv.source-sha256=$image_source_hash" in setup
    assert "org.vvv.security-profile=$VLLM_IMAGE_SECURITY_PROFILE" in setup
    expected_error = (
        'die "docker image $VLLM_IMAGE does not match current pinned sources/security profile"'
    )
    assert expected_error in setup
    assert "already exists, skipping build" not in setup


def test_setup_treats_host_prerequisites_as_preconditions() -> None:
    setup = _read("setup.sh")
    docs = _read("README.md")

    forbidden = [
        "sudo",
        "newgrp docker",
        "loginctl enable-linger",
        "systemctl restart docker",
        "nvidia-cdi-refresh",
    ]
    for value in forbidden:
        assert value not in setup
        assert value not in docs

    assert "This setup expects Docker access to already work for the current user." in setup


def test_setup_has_only_the_current_backend_modes() -> None:
    setup = _read("setup.sh")

    assert "--backend vibevoice|groq" in setup
    assert "--backend must be 'vibevoice' or 'groq'" in setup
    assert "GROQ_API_KEY" in setup
    assert "--groq-api-key is required when --backend is groq" in setup
    assert "--client-ca-cert-path" in setup
    assert "scripts.generate_client_bundle" in setup


def test_teardown_removes_installed_runtime_without_destroying_credentials() -> None:
    teardown = _read("teardown.sh")

    assert "vibevoice-server-container" in teardown
    assert "vibevoice-vllm" in teardown
    assert "vibevoice-backend-netns" in teardown
    assert "vibevoice-vllm:latest" in teardown
    assert "reference=${VLLM_REPOSITORY}:*" in teardown
    assert "label=org.vvv.source-sha256" in teardown
    assert "label=org.vvv.security-profile" in teardown
    assert "docker rm -f" in teardown
    assert "docker image rm --force" in teardown

    assert ".service" in teardown
    assert "daemon-reload" in teardown
    assert "reset-failed" in teardown
    assert "groq.env" in teardown
    assert "/tmp/vibevoice-vendor-" in teardown
    assert "$script_dir/run" not in teardown

    for credential_artifact in [
        "keys/client-cert.pem",
        "keys/client-key.pem",
        "client-bundle.vvv.json",
        "server-spki-pin.txt",
    ]:
        assert credential_artifact not in teardown


def test_hand_run_client_auth_scripts_are_generate_or_validate_not_repair() -> None:
    setup = _read("setup.sh")
    client_generator = _read("scripts/generate_client_cert.py")
    client_validator = _read("scripts/validate_client_cert.py")

    assert "Generating or repairing" not in setup
    assert "Partial mTLS client-auth artifact state exists" in setup
    assert "scripts.validate_client_cert" in setup

    assert "Client-auth artifacts already exist" in client_generator
    assert "validate_client_auth_artifacts" in client_validator


def test_proxy_and_fastapi_backend_are_uds_only_by_construction() -> None:
    setup = _read("setup.sh")
    server_main = _read("server/__main__.py")
    proxy_source = _read("rust_proxy/src/main.rs")
    app_source = _read("server/app.py")

    assert "--upstream-uds" in setup
    assert "--upstream-peer-uid $HOST_UID" in setup
    assert "--upstream-peer-gid $HOST_GID" in setup
    assert "--upstream-peer-uid" in _read("deploy/env.example")
    assert "--upstream-peer-gid" in _read("deploy/env.example")
    assert "--upstream-host" not in setup
    assert "--upstream-port" not in setup
    assert "upstream_host" not in proxy_source
    assert "upstream_port" not in proxy_source
    assert "validate_upstream_socket_path" in proxy_source
    assert "fs::symlink_metadata" in proxy_source
    assert "peer_cred()" in proxy_source

    assert 'parser.add_argument("--host"' not in server_main
    assert 'parser.add_argument("--port"' not in server_main
    assert "host=settings.server_host" not in server_main
    assert "port=settings.server_port" not in server_main
    assert "fd=sock.fileno()" in server_main

    assert "httpx.AsyncClient()" not in app_source
    assert "trust_env=False" in app_source
    assert "follow_redirects=False" in app_source
    assert "http2=False" in app_source


def test_public_proxy_is_ipv4_only_by_construction() -> None:
    setup = _read("setup.sh")
    proxy_source = _read("rust_proxy/src/main.rs")
    docs = _read("README.md")

    assert "--listen-host 0.0.0.0" in setup
    assert "TcpSocket::new_v4()" in proxy_source
    assert "TcpSocket::new_v6()" not in proxy_source
    assert "public listener must be an IPv4 socket address" in proxy_source
    assert "proxy_public_listener_rejects_ipv6" in proxy_source

    assert "RestrictAddressFamilies=AF_INET AF_UNIX" in setup
    assert "AF_INET6" not in setup

    assert "IPv4-only by construction" in docs
    assert "not `[::]`" in docs


def test_public_proxy_requires_mtls_by_construction() -> None:
    setup = _read("setup.sh")
    proxy_source = _read("rust_proxy/src/main.rs")
    client = _read("client/cli.py") + _read("client/client.py")
    docs = _read("README.md")

    assert "--client-ca-cert-path" in setup
    assert "with_client_cert_verifier" in proxy_source
    assert "WebPkiClientVerifier" in proxy_source
    assert "reload_from_pem_file" not in proxy_source

    assert "--client-cert" in client
    assert "--client-key" in client
    assert "--server-pin" in client
    assert "load_cert_chain" in client
    assert "cert=self._cert" not in client
    assert "--client-cert" in docs
    assert "--client-key" in docs
    assert "--server-pin" in docs
    assert "curl -sk" not in docs
    assert "curl -k" not in docs

    forbidden = [
        "keys/client-key.pem:/",
        "$CLIENT_KEY:",
    ]
    for value in forbidden:
        assert value not in setup

    assert "$CLIENT_KEY" in setup


def test_public_clients_use_exact_server_spki_pin_not_ca_or_hostname_authority() -> None:
    setup = _read("setup.sh")
    proxy_source = _read("rust_proxy/src/main.rs")
    cert_generator = _read("scripts/generate_cert.py")
    cli = _read("client/cli.py")
    client = _read("client/client.py")
    docs = _read("README.md")
    env_example = _read("deploy/env.example")

    assert "--server-spki-pin-path" in setup
    assert "--server-spki-pin-path" in env_example
    assert "--server-spki-pin-path" in cert_generator
    assert "SERVER_CERT_COMMON_NAME" in proxy_source
    assert "VVV Sovereign Server" in proxy_source
    assert "subject_alt_names = Vec::new()" in proxy_source
    assert "server_spki_pin_from_cert_path" in proxy_source
    assert "Server SPKI pin" in proxy_source

    assert "server-spki-pin.txt" in setup
    assert "server-spki-pin.txt" in docs
    assert "Android import bundle" in setup
    assert "scripts.generate_client_bundle" in setup
    assert "scripts.generate_client_bundle" in docs
    assert "scripts.generate_client_bundle" in env_example
    assert "pinnedpubkey" in setup
    assert "--pinnedpubkey" in docs
    assert 'printf \'insecure\\npinnedpubkey = "%s"\\ncert = "%s"\\nkey = "%s"\\n\'' in setup

    assert "ssl.create_default_context" not in client
    assert "cafile" not in client
    assert "capath" not in client
    assert "check_hostname = False" in client
    assert "verify_mode = ssl.CERT_NONE" in client
    assert "_spki_pin_from_certificate_der" in client
    assert "_normalize_server_pin" in client
    assert "getpeercert(binary_form=True)" in client
    assert "server public key pin mismatch" in client

    assert "--ca-cert" not in cli
    assert "--ca-cert" not in docs
    assert "public CA" not in docs.lower()
    assert "hostname verification" not in docs.lower()
    assert "certificate SANs do not authorize the server" in docs


def test_user_units_do_not_use_mount_namespace_sandboxing() -> None:
    setup = _read("setup.sh")

    # User-manager mount namespace directives can put the proxy in a different
    # user namespace from Docker's Unix socket and make the only valid upstream
    # path fail closed with EACCES on connect.
    forbidden = [
        "PrivateTmp=",
        "ProtectSystem=",
        "ProtectHome=",
        "ReadWritePaths=",
        "InaccessiblePaths=",
        "ProtectKernelTunables=",
        "ProtectControlGroups=",
    ]
    for directive in forbidden:
        assert directive not in setup


def test_public_proxy_uses_mtls_identity_without_application_auth() -> None:
    setup = _read("setup.sh")
    proxy_source = _read("rust_proxy/src/main.rs")
    docs = _read("README.md")

    auth_rejection = "req.headers().contains_key(header::AUTHORIZATION)"
    health_check = 'req.uri().path() == "/health"'
    assert proxy_source.index(auth_rejection) < proxy_source.index(health_check)
    assert "client_identity_from_peer_certs" in proxy_source
    assert "peer_certificates()" in proxy_source
    assert "CLIENT_IDENTITY_HEADER" in proxy_source
    assert "state.auth_verifier" not in proxy_source

    assert 'pinnedpubkey = "%s"' in setup
    assert "read_server_pin_for_curl" in setup
    assert '--config "$tls_config" -s -o "$body_file" -w' in setup

    assert "no auth required" not in docs.lower()
    assert "| GET | `/health` | mTLS |" in docs


def test_e2e_workflow_exercises_public_proxy_security_contract() -> None:
    workflow = _read(".github/workflows/e2e.yml")

    assert "workflow_dispatch:" in workflow
    assert "--uds" in workflow
    assert "--require-https true" in workflow
    assert "--require-https false" not in workflow
    assert "--upstream-uds" in workflow
    assert "--upstream-peer-uid" in workflow
    assert "--upstream-peer-gid" in workflow
    assert "--listen-host 127.0.0.1" in workflow

    assert "--server-pin" in workflow
    assert "--client-cert" in workflow
    assert "--client-key" in workflow
    assert "--pinnedpubkey" in workflow
    assert "--tlsv1.3 --tls-max 1.3" in workflow
    assert "Test: no client certificate is rejected at TLS" in workflow
    assert "Test: wrong server public key pin is rejected client-side" in workflow
    assert "Test: authorization header rejected" in workflow
    assert "Test: backend UDS rejects direct protected non-HTTPS request" in workflow

    assert '--server "http://127.0.0.1:${VVV_PORT}"' not in workflow
    assert "http://127.0.0.1:${VVV_PORT}/" not in workflow
    assert "--ca-cert" not in workflow
    assert "steps.auth" not in workflow


def test_rust_proxy_excludes_unused_public_transport_stacks() -> None:
    cargo_toml = _read("rust_proxy/Cargo.toml")
    cargo_lock = _read("rust_proxy/Cargo.lock")
    proxy_source = _read("rust_proxy/src/main.rs")

    assert "reqwest" not in cargo_toml
    assert "axum" not in cargo_toml
    assert "axum-server" not in cargo_toml
    assert "UnixStream::connect" in proxy_source
    assert "hyper::client::conn::http1::handshake" in proxy_source
    assert "http1::Builder::new()" in proxy_source
    assert ".serve_connection(TokioIo::new(tls_stream), service)" in proxy_source
    assert "TlsAcceptor::from(config)" in proxy_source
    assert "try_public_connection_permit(&permits)" in proxy_source
    assert "tasks.try_join_next()" in proxy_source
    assert "config.alpn_protocols = vec![TLS_ALPN_HTTP1.to_vec()]" in proxy_source
    assert "is_http_upgrade(req.headers())" in proxy_source

    forbidden_lock_packages = [
        'name = "axum"',
        'name = "axum-core"',
        'name = "axum-macros"',
        'name = "axum-server"',
        'name = "h2"',
        'name = "tower"',
        'name = "tower-http"',
        'name = "matchit"',
        'name = "mime"',
        'name = "reqwest"',
        'name = "url"',
        'name = "idna"',
        'name = "icu_normalizer"',
        'name = "tokio-tungstenite"',
        'name = "hyper-rustls"',
        'name = "webpki-roots"',
        'name = "native-tls"',
        'name = "openssl"',
        'name = "aws-lc-rs"',
        'name = "aws-lc-sys"',
    ]
    for package in forbidden_lock_packages:
        assert package not in cargo_lock


def test_python_runtime_dependencies_are_exactly_pinned() -> None:
    pyproject = tomllib.loads(_read("pyproject.toml"))
    dependency_groups = pyproject["dependency-groups"]
    dependencies = [
        *pyproject["project"]["dependencies"],
        *dependency_groups["dev"],
        *dependency_groups["vibevoice-image"],
    ]

    for dependency in dependencies:
        assert "==" in dependency
        assert all(operator not in dependency for operator in (">=", "~=", ">", "<", "*"))


def test_docker_python_runtime_install_is_hash_checked_from_lockfile() -> None:
    dockerfile = _read("Dockerfile")

    assert "COPY pyproject.toml uv.lock /build/vvv/" in dockerfile
    assert "tomllib.load(f)" in dockerfile
    assert "root = packages[\"vibe-voice-vendor\"]" in dockerfile
    assert "--require-hashes" in dockerfile
    assert "--only-binary=:all:" in dockerfile
    assert "package[\"sdist\"][\"hash\"]" in dockerfile
    assert "wheel[\"hash\"]" in dockerfile
    assert "/tmp/vvv-constraints.txt" not in dockerfile


def test_vibevoice_plugin_install_does_not_resolve_unpinned_dependencies() -> None:
    dockerfile = _read("Dockerfile")
    install_command = (
        "pip install --no-cache-dir --no-deps --no-build-isolation /build/VibeVoice"
    )

    assert install_command in dockerfile
    assert "pip install --no-cache-dir /build/VibeVoice" not in dockerfile


def test_vllm_audio_runtime_dependencies_are_pinned_and_verified() -> None:
    pyproject = tomllib.loads(_read("pyproject.toml"))
    image_dependencies = set(pyproject["dependency-groups"]["vibevoice-image"])
    dockerfile = _read("Dockerfile")

    for dependency in [
        "librosa==0.11.0",
        "scipy==1.16.3",
        "soundfile==0.13.1",
        "scikit-learn==1.8.0",
        "soxr==1.0.0",
        "threadpoolctl==3.6.0",
    ]:
        assert dependency in image_dependencies

    assert "import librosa" in dockerfile
    assert "import scipy.signal" in dockerfile
    assert "import soundfile" in dockerfile
    assert "from vllm.multimodal import audio as vllm_audio" in dockerfile
    assert 'module.__class__.__name__ == "PlaceholderModule"' in dockerfile
    assert "vLLM audio dependency" in dockerfile


def test_vibevoice_ffmpeg_decode_is_bounded_in_runtime_image() -> None:
    dockerfile = _read("Dockerfile")

    assert 'text.count(thread_old) != 2' in dockerfile
    assert "VibeVoice ffprobe protocol patch did not match exactly once" in dockerfile
    assert "VibeVoice ffprobe runner patch did not match exactly once" in dockerfile
    assert "VibeVoice file ffmpeg protocol patch did not match exactly once" in dockerfile
    assert "VibeVoice pipe ffmpeg protocol patch did not match exactly once" in dockerfile
    assert '"-protocol_whitelist", "file",' in dockerfile
    assert '"-protocol_whitelist", "file,pipe",' in dockerfile
    assert '"-protocol_whitelist", "pipe",' in dockerfile
    assert "_run_ffmpeg(cmd_probe)" in dockerfile
    assert 'os.getenv("VIBEVOICE_FFMPEG_THREADS", "1")' in dockerfile
    assert "VIBEVOICE_FFMPEG_MAX_CONCURRENCY=1" in dockerfile
    assert "VIBEVOICE_FFMPEG_THREADS=1" in dockerfile
    assert "VIBEVOICE_FFMPEG_TIMEOUT_SECONDS=900" in dockerfile
    assert "timeout=timeout" in dockerfile
    assert "VIBEVOICE_FFMPEG_MAX_CONCURRENCY=64" not in dockerfile
