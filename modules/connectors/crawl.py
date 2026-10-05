from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlsplit
from uuid import UUID, uuid4

import httpx
from bs4 import BeautifulSoup
from crawlee import ConcurrencySettings
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from core.config import Settings
from modules.connectors.n8n import read_rss
from modules.connectors.public import CrawlRequest, RSSRequest, validate_public_url
from modules.connectors.registry import normalize

MAX_BYTES = 25 * 1024 * 1024
_job_lock = asyncio.Lock()
app = FastAPI(title="BBD-OS bounded browser collector")
_SERVICE_INSTANCE_ID = str(uuid4())
_agent_read_tasks: dict[UUID, tuple[AgentReadRequest, asyncio.Task[list[dict[str, object]]]]] = {}
_BROWSER_CONTROL_URL = "http://172.29.250.10:8000/api/v1/browser-control"


class AgentReadRequest(BaseModel):
    """Carry one bounded static-read operation and its source-derived scope."""

    model_config = ConfigDict(extra="forbid", strict=True)
    job_id: UUID
    operation_id: UUID
    claim_generation: StrictInt = Field(ge=1)
    service_instance_id: str = Field(min_length=1, max_length=128)
    job_token: str = Field(pattern=r"^[a-f0-9]{64}$")
    target_url: str = Field(min_length=1, max_length=2048)
    origin: str = Field(min_length=9, max_length=512)
    path_prefix: str = Field(min_length=1, max_length=2048)
    max_pages: StrictInt = Field(ge=1, le=3)
    timeout_seconds: StrictInt = Field(ge=1, le=45)


class AgentReadCancel(BaseModel):
    """Identify one service-owned task for authenticated cleanup."""

    model_config = ConfigDict(extra="forbid", strict=True)
    job_id: UUID
    operation_id: UUID
    claim_generation: StrictInt = Field(ge=1)
    service_instance_id: str = Field(min_length=1, max_length=128)
    job_token: str = Field(pattern=r"^[a-f0-9]{64}$")


async def _dns_safe(url: str) -> None:
    """Reject URLs whose current DNS resolution includes non-public addresses.

    This resolves and validates at check time; it does not pin the result to the
    later socket connection, so it is not a complete DNS-rebinding defense.
    """
    await validate_public_url(url)


async def preview_rss(payload: RSSRequest) -> dict[str, object]:
    """Return an RSS preview under the single-job lock and a 60-second bound."""
    if _job_lock.locked():
        raise HTTPException(status_code=429, detail="A browser job is already running")
    async with _job_lock:
        async with asyncio.timeout(60):
            return await read_rss(str(payload.url), payload.cursor)


async def crawl(payload: CrawlRequest) -> list[dict[str, Any]]:
    """Collect bounded HTTP or browser pages with URL and byte-budget checks.

    A process-local lock serializes jobs; timeout, page count, depth, and
    aggregate response bytes bound collection work.
    """
    if _job_lock.locked():
        raise HTTPException(status_code=429, detail="A browser job is already running")
    async with _job_lock:
        await _dns_safe(str(payload.url))
        records: list[dict[str, Any]] = []
        state = {"bytes": 0}
        deadline = timedelta(seconds=payload.timeout_seconds)

        async def append(url: str, content: str, size: int) -> None:
            """Account for content and append its normalized observation."""
            state["bytes"] += size
            if state["bytes"] > MAX_BYTES:
                raise HTTPException(status_code=413, detail="Browser download limit exceeded")
            records.append(
                normalize(
                    {
                        "provider_id": url,
                        "content": content[:200_000],
                        "observed_at": datetime.now(UTC).isoformat(),
                        "metadata": {"url": url},
                    }
                )
            )

        async with asyncio.timeout(deadline.total_seconds()):
            if payload.mode == "http":
                queue = [(str(payload.url), 0)]
                visited: set[str] = set()
                async with httpx.AsyncClient(
                    timeout=deadline.total_seconds(), follow_redirects=False, trust_env=False
                ) as client:
                    while queue and len(records) < payload.max_pages:
                        request_url, depth = queue.pop(0)
                        if request_url in visited:
                            continue
                        current_url = request_url
                        response: httpx.Response | None = None
                        for _ in range(6):
                            # Revalidate every redirect target immediately before its request.
                            await _dns_safe(current_url)
                            visited.add(current_url)
                            response = await client.send(client.build_request("GET", current_url, headers={"Accept": "text/html,application/xhtml+xml"}), stream=True)
                            if response.status_code not in {301, 302, 303, 307, 308}:
                                break
                            location = response.headers.get("location")
                            await response.aclose()
                            if not location:
                                raise ValueError("Web redirect has no location")
                            current_url = urljoin(current_url, location)
                            await _dns_safe(current_url)
                        else:
                            raise ValueError("Web redirect limit exceeded")
                        if response is None:
                            raise ValueError("Web response was not received")
                        async with response:
                            response.raise_for_status()
                            length = response.headers.get("content-length")
                            if length and int(length) > MAX_BYTES - state["bytes"]:
                                raise HTTPException(status_code=413, detail="Browser download limit exceeded")
                            body = bytearray()
                            async for chunk in response.aiter_bytes():
                                state["bytes"] += len(chunk)
                                if state["bytes"] > MAX_BYTES:
                                    raise HTTPException(status_code=413, detail="Browser download limit exceeded")
                                body.extend(chunk)
                        final_url = str(response.url)
                        soup = BeautifulSoup(bytes(body), "html.parser")
                        await append(final_url, soup.get_text("\n", strip=True), 0)
                        if depth < payload.max_depth:
                            for anchor in soup.find_all("a", href=True):
                                candidate = urljoin(final_url, str(anchor["href"]))
                                if urlsplit(candidate).scheme in {"http", "https"} and candidate not in visited and len(queue) < 100:
                                    queue.append((candidate, depth + 1))
            else:
                from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext

                crawler = PlaywrightCrawler(
                    max_requests_per_crawl=payload.max_pages,
                    max_crawl_depth=payload.max_depth,
                    max_request_retries=0,
                    request_handler_timeout=deadline,
                    navigation_timeout=deadline,
                    concurrency_settings=ConcurrencySettings(max_concurrency=1, max_tasks_per_minute=60),
                    headless=True,
                    configure_logging=False,
                )

                response_tasks: list[asyncio.Task[None]] = []
                cdp_sessions: list[Any] = []
                response_budget_lock = asyncio.Lock()

                async def guard(context: Any) -> None:
                    """Install request and response guards before browser navigation."""
                    await _dns_safe(context.request.url)

                    async def check_request(route: Any) -> None:
                        """Abort requests whose current DNS result is non-public.

                        The check does not pin resolved addresses to the browser's
                        eventual socket connection.
                        """
                        try:
                            if state.get("exceeded"):
                                await route.abort()
                                return
                            await _dns_safe(route.request.url)
                            await route.continue_()
                        except (ValueError, OSError):
                            await route.abort()

                    await context.page.route("**/*", check_request)
                    cdp = await context.page.context.new_cdp_session(context.page)
                    cdp_sessions.append(cdp)
                    await cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Response"}]})

                    async def intercept_response(paused: dict[str, Any]) -> None:
                        """Enforce the shared response byte budget for browser traffic."""
                        request_id = str(paused["requestId"])
                        async with response_budget_lock:
                            if state.get("exceeded"):
                                await cdp.send("Fetch.failRequest", {"requestId": request_id, "errorReason": "Aborted"})
                                return
                            try:
                                response_status = int(paused.get("responseStatusCode", 200))
                                if 300 <= response_status < 400:
                                    await cdp.send("Fetch.continueResponse", {"requestId": request_id})
                                    return
                                stream = await cdp.send("Fetch.takeResponseBodyAsStream", {"requestId": request_id})
                                handle = str(stream["stream"])
                                body = bytearray()
                                remaining = MAX_BYTES - state["bytes"]
                                while True:
                                    chunk = await cdp.send(
                                        "IO.read", {"handle": handle, "size": min(64 * 1024, remaining - len(body) + 1)}
                                    )
                                    data = (
                                        base64.b64decode(chunk["data"])
                                        if chunk.get("base64Encoded")
                                        else str(chunk.get("data", "")).encode("utf-8")
                                    )
                                    if len(body) + len(data) > remaining:
                                        state["exceeded"] = True
                                        await cdp.send("IO.close", {"handle": handle})
                                        await cdp.send(
                                            "Fetch.failRequest", {"requestId": request_id, "errorReason": "Aborted"}
                                        )
                                        return
                                    body.extend(data)
                                    if chunk.get("eof"):
                                        break
                                await cdp.send("IO.close", {"handle": handle})
                                headers = [
                                    header
                                    for header in paused.get("responseHeaders", [])
                                    if header.get("name", "").lower() not in {"content-length", "transfer-encoding"}
                                ]
                                headers.append({"name": "Content-Length", "value": str(len(body))})
                                await cdp.send(
                                    "Fetch.fulfillRequest",
                                    {
                                        "requestId": request_id,
                                        "responseCode": response_status,
                                        "responseHeaders": headers,
                                        "body": base64.b64encode(body).decode("ascii"),
                                    },
                                )
                                state["bytes"] += len(body)
                            except Exception:
                                state["interception_error"] = True
                                try:
                                    await cdp.send(
                                        "Fetch.failRequest", {"requestId": request_id, "errorReason": "Aborted"}
                                    )
                                except Exception:
                                    pass

                    cdp.on(
                        "Fetch.requestPaused",
                        lambda paused: response_tasks.append(asyncio.create_task(intercept_response(paused))),
                    )

                @crawler.router.default_handler
                async def handle_browser(context: PlaywrightCrawlingContext) -> None:
                    """Extract bounded visible page text and enqueue same-host links."""
                    if state.get("exceeded"):
                        raise HTTPException(status_code=413, detail="Browser download limit exceeded")
                    page = context.page
                    text = await page.locator("body").inner_text(timeout=payload.timeout_seconds * 1000)
                    await append(page.url, text, 0)
                    await context.enqueue_links()

                crawler.pre_navigation_hook(guard)
                await crawler.run([str(payload.url)])
                if response_tasks:
                    await asyncio.gather(*response_tasks, return_exceptions=True)
                for cdp in cdp_sessions:
                    try:
                        await cdp.detach()
                    except Exception:
                        pass
                if state.get("exceeded"):
                    raise HTTPException(status_code=413, detail="Browser download limit exceeded")
                if state.get("interception_error"):
                    raise HTTPException(status_code=502, detail="Browser response could not be bounded")
        return records


def _agent_target_allowed(url: str, origin: str, path_prefix: str) -> bool:
    """Require a credential-free HTTPS target within the granted origin/path segments."""
    from urllib.parse import unquote

    try:
        parsed = urlsplit(url)
        port = parsed.port
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return False
    path = unquote(parsed.path or "/")
    prefix = unquote(path_prefix or "/")
    return bool(
        parsed.scheme == "https" and parsed.username is None and parsed.password is None
        and parsed.fragment == "" and parsed.query == "" and "?" not in url and "#" not in url
        and f"https://{host}" == origin
        and (port is None or port == 443)
        and not any(segment in {".", ".."} for segment in path.split("/"))
        and "%2f" not in parsed.path.lower() and "%5c" not in parsed.path.lower()
        and "%25" not in parsed.path.lower()
        and "\\" not in path
        and (path == prefix or prefix == "/" or path.startswith(prefix.rstrip("/") + "/"))
        and len(url.encode("utf-8")) <= 2048
    )


async def _browser_control_callback(
    settings: Settings,
    payload: AgentReadRequest,
    event: str,
    *,
    request_ordinal: int = 0,
    target_url: str | None = None,
    actual_pages: int = 0,
    actual_bytes: int = 0,
    result_hash: str | None = None,
) -> bool:
    """Ask the fixed API origin for current authority using service and per-job tokens."""
    body = {
        "event": event,
        "operation_id": str(payload.operation_id),
        "claim_generation": payload.claim_generation,
        "service_instance_id": payload.service_instance_id,
        "request_ordinal": request_ordinal,
        "target_url": target_url,
        "actual_pages": actual_pages,
        "actual_bytes": actual_bytes,
        "result_hash": result_hash,
    }
    shared = settings.browser_shared_token.get_secret_value()
    if not shared:
        return False
    try:
        async with httpx.AsyncClient(
            timeout=2, trust_env=False, follow_redirects=False,
        ) as client:
            response = await client.post(
                f"{_BROWSER_CONTROL_URL}/jobs/{payload.job_id}/event",
                json=body,
                headers={
                    "Authorization": f"Bearer {shared}",
                    "X-Browser-Job-Token": payload.job_token,
                },
            )
            if response.status_code != 200:
                return False
            data = response.json()
            return data.get("allowed") is True
    except (httpx.HTTPError, ValueError, TypeError):
        return False


async def _execute_static_agent_read(
    payload: AgentReadRequest, settings: Settings
) -> list[dict[str, object]]:
    """Fetch static pages only after per-request API permits and pinned public HTTPS resolution."""
    import hashlib
    import json
    import time
    from bs4 import BeautifulSoup
    from httpx2 import AsyncClient
    from modules.tools.mcp_transport import McpOperationNetworkBudget, McpPinnedHttpTransport

    if (
        payload.service_instance_id != _SERVICE_INSTANCE_ID
        or not _agent_target_allowed(payload.target_url, payload.origin, payload.path_prefix)
    ):
        raise ValueError("Browser target is outside the service instance or source scope")
    if not await _browser_control_callback(settings, payload, "register"):
        raise PermissionError("Browser network capability or authority is unavailable")
    deadline = time.monotonic() + payload.timeout_seconds
    budget = McpOperationNetworkBudget(
        deadline=deadline, max_requests=6, max_request_bytes=8192,
        max_response_bytes=5 * 1024 * 1024, max_inflight_requests=1,
        max_teardown_requests=1, max_teardown_bytes=8192, before_request=None,
    )
    pages: list[dict[str, object]] = []
    queue = [(payload.target_url, 0)]
    visited: set[str] = set()
    total_bytes = 0
    page_bytes = 0  # persisted page bodies only; redirect bodies are not evidence
    request_ordinal = 0

    while queue and len(pages) < payload.max_pages:
        requested, depth = queue.pop(0)
        if requested in visited or not _agent_target_allowed(requested, payload.origin, payload.path_prefix):
            continue
        redirects = 0
        current = requested
        while True:
            request_ordinal += 1
            if request_ordinal > 6:
                raise ValueError("Browser request limit exceeded")
            if not _agent_target_allowed(current, payload.origin, payload.path_prefix):
                raise PermissionError("Browser redirect left the granted source scope")

            async def permit() -> bool:
                """Obtain an authoritative permit for this pinned target immediately before socket send."""
                return await _browser_control_callback(
                    settings, payload, "authorize",
                    request_ordinal=request_ordinal, target_url=current,
                )

            budget.before_request = permit
            transport = McpPinnedHttpTransport(current, budget)
            async with AsyncClient(
                transport=transport, follow_redirects=False,
                timeout=max(0.1, budget.remaining_seconds()), trust_env=False,
            ) as client:
                response = await client.get(
                    current,
                    headers={"Accept": "text/html,application/xhtml+xml", "Accept-Encoding": "identity"},
                )
                body = await response.aread()
                status = response.status_code
                headers = response.headers
                final_url = str(response.url)
            total_bytes += len(body)
            if total_bytes > 5 * 1024 * 1024:
                raise ValueError("Browser response byte budget exceeded")
            visited.add(current)
            if status in {301, 302, 303, 307, 308}:
                redirects += 1
                if redirects > 2:
                    raise ValueError("Browser redirect limit exceeded")
                location = headers.get("location")
                if not location:
                    raise ValueError("Browser redirect has no location")
                current = urljoin(current, location)
                continue
            if status >= 400:
                raise ValueError("Browser source returned an unsuccessful status")
            if headers.get("content-type", "").split(";", 1)[0].strip().lower() not in {
                "text/html", "application/xhtml+xml",
            }:
                raise ValueError("Browser source did not return a static HTML page")
            soup = BeautifulSoup(body, "html.parser")
            # Bound by UTF-8 bytes to match the worker and the DB octet_length check.
            extracted = soup.get_text("\n", strip=True).encode("utf-8")[:20_000].decode("utf-8", "ignore")
            page_bytes += len(body)
            page = {
                "requested_url": requested,
                "final_url": final_url,
                "observed_at": datetime.now(UTC).isoformat(),
                "content_digest": hashlib.sha256(body).hexdigest(),
                "raw_content": base64.b64encode(body).decode("ascii"),
                "extracted_text": extracted,
            }
            pages.append(page)
            if depth == 0 and len(pages) < payload.max_pages:
                for anchor in soup.find_all("a", href=True):
                    candidate = urljoin(final_url, str(anchor["href"]))
                    if (
                        candidate not in visited
                        and _agent_target_allowed(candidate, payload.origin, payload.path_prefix)
                        and len(queue) < 32
                    ):
                        queue.append((candidate, 1))
            break

    if not pages:
        raise ValueError("Browser source returned no pages within the granted scope")
    result_digest = hashlib.sha256(json.dumps(
        [{k: v for k, v in page.items() if k != "raw_content"} for page in pages],
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    if not await _browser_control_callback(
        settings, payload, "complete", request_ordinal=request_ordinal,
        actual_pages=len(pages), actual_bytes=page_bytes, result_hash=result_digest,
    ):
        raise PermissionError("Browser completion could not be acknowledged")
    return pages


def _job_tokens_match(payload: AgentReadRequest | AgentReadCancel, header_token: str | None) -> bool:
    """Require the per-operation secret both in the validated body and control header."""
    import hmac

    return bool(
        header_token and hmac.compare_digest(payload.job_token, header_token)
    )


@app.get("/agent-reads/instance")
async def agent_read_instance(authorization: str | None = Header(default=None)) -> dict[str, str]:
    """Return this singleton service's stable process identity to the authenticated worker."""
    settings = Settings()
    scheme, _, token = (authorization or "").partition(" ")
    expected = settings.browser_shared_token.get_secret_value()
    if not expected or scheme.lower() != "bearer" or token != expected:
        raise HTTPException(status_code=401, detail="Browser service authentication required")
    return {"service_instance_id": _SERVICE_INSTANCE_ID}


@app.post("/agent-reads")
async def agent_read(
    payload: AgentReadRequest,
    authorization: str | None = Header(default=None),
    job_token: str | None = Header(default=None, alias="X-Browser-Job-Token"),
) -> dict[str, object]:
    """Run one tracked static read under the same singleton lock as crawl and RSS preview."""
    settings = Settings()
    scheme, _, token = (authorization or "").partition(" ")
    expected = settings.browser_shared_token.get_secret_value()
    if (
        not expected or scheme.lower() != "bearer" or token != expected
        or not _job_tokens_match(payload, job_token)
    ):
        raise HTTPException(status_code=401, detail="Browser operation authentication required")
    if payload.service_instance_id != _SERVICE_INSTANCE_ID:
        raise HTTPException(status_code=409, detail="Browser service instance changed")
    if _job_lock.locked():
        raise HTTPException(status_code=429, detail="A browser job is already running")
    async with _job_lock:
        task = asyncio.create_task(_execute_static_agent_read(payload, settings))
        _agent_read_tasks[payload.job_id] = (payload, task)
        try:
            async with asyncio.timeout(payload.timeout_seconds + 2):
                pages = await task
            import json

            wire = json.dumps(pages, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(wire) > 8 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="Browser result envelope exceeded its limit")
            return {
                "job_id": str(payload.job_id),
                "operation_id": str(payload.operation_id),
                "claim_generation": payload.claim_generation,
                "service_instance_id": _SERVICE_INSTANCE_ID,
                "pages": pages,
            }
        except TimeoutError as exc:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await asyncio.shield(_browser_control_callback(settings, payload, "cancelled"))
            raise HTTPException(status_code=504, detail="Browser read timed out") from exc
        except asyncio.CancelledError:
            await asyncio.shield(_browser_control_callback(settings, payload, "cancelled"))
            raise
        except PermissionError as exc:
            await asyncio.shield(_browser_control_callback(settings, payload, "cancelled"))
            raise HTTPException(status_code=403, detail="Browser read was denied") from exc
        except (ValueError, OSError) as exc:
            await asyncio.shield(_browser_control_callback(settings, payload, "cancelled"))
            raise HTTPException(status_code=422, detail="Browser page could not be read") from exc
        except Exception as exc:
            await asyncio.shield(_browser_control_callback(settings, payload, "cancelled"))
            raise HTTPException(status_code=502, detail="Browser operation did not complete") from exc
        finally:
            _agent_read_tasks.pop(payload.job_id, None)


@app.get("/agent-reads/{job_id}/status")
async def agent_read_status(
    job_id: UUID,
    authorization: str | None = Header(default=None),
    job_token: str | None = Header(default=None, alias="X-Browser-Job-Token"),
) -> dict[str, object]:
    """Report one bounded process-local service status for exact job reconciliation."""
    settings = Settings()
    scheme, _, token = (authorization or "").partition(" ")
    expected = settings.browser_shared_token.get_secret_value()
    task_item = _agent_read_tasks.get(job_id)
    if (
        not expected or scheme.lower() != "bearer" or token != expected
        or task_item is None or not _job_tokens_match(task_item[0], job_token)
    ):
        raise HTTPException(status_code=404, detail="Browser job status unavailable")
    return {
        "job_id": str(job_id), "service_instance_id": _SERVICE_INSTANCE_ID,
        "status": "running",
    }


@app.post("/agent-reads/{job_id}/cancel")
async def cancel_agent_read(
    job_id: UUID,
    payload: AgentReadCancel,
    authorization: str | None = Header(default=None),
    job_token: str | None = Header(default=None, alias="X-Browser-Job-Token"),
) -> dict[str, object]:
    """Cancel one exact active service task, await cleanup, then acknowledge through the API callback."""
    settings = Settings()
    scheme, _, token = (authorization or "").partition(" ")
    expected = settings.browser_shared_token.get_secret_value()
    item = _agent_read_tasks.get(job_id)
    if (
        not expected or scheme.lower() != "bearer" or token != expected
        or payload.job_id != job_id or payload.operation_id != (item[0].operation_id if item else None)
        or payload.claim_generation != (item[0].claim_generation if item else None)
        or payload.service_instance_id != _SERVICE_INSTANCE_ID
        or item is None or not _job_tokens_match(payload, job_token)
        or not _job_tokens_match(item[0], job_token)
    ):
        raise HTTPException(status_code=404, detail="Browser job is not active")
    task = item[1]
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        # The task failed at the same time as cancellation; the authenticated
        # callback below still has to prove cleanup before this endpoint replies.
        pass
    if not await _browser_control_callback(settings, item[0], "cancelled"):
        raise HTTPException(status_code=503, detail="Browser cleanup could not be acknowledged")
    return {"job_id": str(job_id), "service_instance_id": _SERVICE_INSTANCE_ID, "cleaned": True}


@app.post("/crawl")
async def collect(
    payload: CrawlRequest, authorization: str | None = Header(default=None)
) -> list[dict[str, Any]]:
    """Serve authenticated browser collection requests with bounded crawl work."""
    settings = Settings()
    scheme, _, token = (authorization or "").partition(" ")
    expected = settings.browser_shared_token.get_secret_value()
    if not expected or scheme.lower() != "bearer" or token != expected:
        raise HTTPException(status_code=401, detail="Browser service authentication required")
    try:
        return await crawl(payload)
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="Browser job timed out") from exc
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="Invalid browser job") from exc


@app.post("/rss")
async def collect_rss(
    payload: RSSRequest, authorization: str | None = Header(default=None)
) -> dict[str, object]:
    """Serve authenticated RSS preview requests through the bounded collector."""
    settings = Settings()
    scheme, _, token = (authorization or "").partition(" ")
    expected = settings.browser_shared_token.get_secret_value()
    if not expected or scheme.lower() != "bearer" or token != expected:
        raise HTTPException(status_code=401, detail="Browser service authentication required")
    try:
        return await preview_rss(payload)
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="RSS job timed out") from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=422, detail="RSS source could not be collected") from exc
