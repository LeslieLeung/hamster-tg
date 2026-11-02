import asyncio
from functools import partial


async def blocking(function, *args, executor=None, on_cancel=None):
    """Keep the caller's locks until the actual thread operation has finished."""
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(executor, partial(function, *args))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(future)
            break
        except asyncio.CancelledError:
            cancelled = True
            if on_cancel:
                on_cancel()
            if future.cancelled():
                raise
        except Exception:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    if cancelled:
        raise asyncio.CancelledError
    return result
