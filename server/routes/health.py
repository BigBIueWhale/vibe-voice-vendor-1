import httpx
from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/health")
async def health(request: Request) -> dict[str, str]:
    http_client: httpx.AsyncClient = request.app.state.http_client
    vllm_url: str = request.app.state.settings.vllm_base_url

    try:
        resp = await http_client.get(f"{vllm_url}/health", timeout=5.0)
        vllm_status = "ok" if resp.status_code == 200 else "degraded"
    except Exception:
        vllm_status = "unreachable"

    return {"status": "ok", "vllm": vllm_status}
