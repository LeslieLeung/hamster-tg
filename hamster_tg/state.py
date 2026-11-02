import asyncio

api_rate_lock = asyncio.Lock()
next_api_request_at_monotonic = 0.0
