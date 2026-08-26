"""The `remote_openai` backend — a hosted provider, or our own remote deployment.

The thinnest backend (`docs/BACKENDS.md` §2.3): `activate` and `stop` are no-ops
beyond verifying the model exists upstream, and the control plane becomes a thin
auth and admin layer rather than a supervisor. That asymmetry is fine — the
contract is what matters.

The upstream key comes from the environment, never from `models.yaml`.
"""

import httpx

from harness_control.catalog import ModelSpec
from harness_control.supervisor.backends.base import BackendError, ResourceInfo

_TIMEOUT_S = 5.0


class RemoteOpenAIBackend:
    name = "remote_openai"

    def __init__(
        self,
        url: str,
        api_key: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=_TIMEOUT_S)
        self._owns_client = client is None
        self._model_ref: str | None = None

    @property
    def base_url(self) -> str:
        return self._url

    async def activate(self, spec: ModelSpec) -> None:
        """Verify the model is offered upstream. There is nothing to load."""
        if not self._url:
            raise BackendError("HARNESS_REMOTE_BASE_URL is not set")
        available = await self._list_models()
        if spec.model_ref not in available:
            raise BackendError(
                f"{spec.model_ref!r} is not offered by {self._url}; "
                f"upstream lists {sorted(available)[:10]}"
            )
        self._model_ref = spec.model_ref

    async def stop(self) -> None:
        """Nothing was acquired, so nothing is released. Idempotent by construction."""
        self._model_ref = None

    async def health(self) -> bool:
        if self._model_ref is None:
            return False
        try:
            return self._model_ref in await self._list_models()
        except BackendError:
            return False

    async def progress_hint(self) -> str | None:
        """Activation is instant, so there is never progress to report."""
        return None

    async def resources(self) -> list[ResourceInfo]:
        """Someone else's hardware. We have no visibility and should not pretend."""
        return []

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _list_models(self) -> set[str]:
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        try:
            response = await self._client.get(
                f"{self._url}/v1/models", headers=headers, timeout=_TIMEOUT_S
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise BackendError(
                f"{self._url}/v1/models returned HTTP {exc.response.status_code}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise BackendError(f"{self._url} is unreachable: {exc}") from exc

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return set()
        return {
            entry["id"]
            for entry in data
            if isinstance(entry, dict) and isinstance(entry.get("id"), str)
        }
