"""ASGI finalization for durable API activity receipts."""

from typing import Any

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BackupActivityMiddleware:
    """Keep an admitted owner request active through the final streamed response byte."""

    def __init__(self, app: ASGIApp) -> None:
        """Store the wrapped application whose complete response lifetime is tracked."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass through non-HTTP scopes and settle HTTP activity after ASGI completion."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        response_status = 500
        finalized = False
        send_failed = False

        async def finalize(*, uncertain: bool, interrupted: bool = False) -> None:
            """Finish this request's durable receipt once after its response lifetime."""
            nonlocal finalized
            if finalized:
                return
            finalized = True
            receipt = getattr(request.state, "backup_activity", None)
            if receipt is None:
                return
            factory: Any = request.app.state.session_factory
            async with factory() as session:
                from modules.settings.public import finish_activity

                await finish_activity(session, receipt, uncertain=uncertain, interrupted=interrupted)
                await session.commit()

        async def send_and_finalize(message: Message) -> None:
            """Forward one ASGI message while recording status and transport failure."""
            nonlocal response_status, send_failed
            try:
                await send(message)
            except BaseException:
                send_failed = True
                raise
            if message["type"] == "http.response.start":
                response_status = int(message["status"])

        try:
            await self.app(scope, receive, send_and_finalize)
        except BaseException:
            # This receipt tracks only the API request lifetime. Effect-owning callbacks
            # maintain their own durable activity, which remains uncertain after cancellation.
            await finalize(uncertain=False, interrupted=True)
            raise
        finally:
            # Wait for the entire ASGI app call, including streaming generators and response
            # background tasks, before settling this request identity. A client send failure
            # is recorded as interrupted; it is not evidence that an external effect is unknown.
            if not finalized:
                await finalize(uncertain=False, interrupted=send_failed or response_status >= 500)
