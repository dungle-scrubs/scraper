"""Loopback HTTP body stall injected only into the deadline regression fixture."""

STALL_SETUP = """
async def stalled_body(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    request = await reader.readuntil(b"\\r\\n\\r\\n")
    content_type = "text/html" if b"/stalled-document " in request else "application/javascript"
    writer.write(
        f"HTTP/1.1 200 OK\\r\\nContent-Type: {content_type}\\r\\n"
        "Content-Length: 65537\\r\\nConnection: close\\r\\n\\r\\n".encode()
        + b" " * 65536
    )
    await writer.drain()
    marker = RECORD.parent / "stalled-body.json"
    marker.write_text(json.dumps({"sent": 65536, "closed": False}))
    try:
        await reader.read()
        marker.write_text(json.dumps({"sent": 65536, "closed": True}))
    finally:
        writer.close()
        await writer.wait_closed()


class StalledBodyTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.real = httpx.AsyncHTTPTransport()
        self.synthetic = httpx.MockTransport(respond)
        self.servers: list[asyncio.Server] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path not in ("/stalled.js", "/stalled-document"):
            return await self.synthetic.handle_async_request(request)
        # Only the external transport changes; URL and DNS enforcement stay real.
        server = await asyncio.start_server(stalled_body, "127.0.0.1", 0)
        self.servers.append(server)
        port = server.sockets[0].getsockname()[1]
        local = httpx.Request(
            request.method,
            f"http://127.0.0.1:{port}{request.url.path}",
            headers=request.headers,
            extensions=request.extensions,
        )
        return await self.real.handle_async_request(local)

    async def aclose(self) -> None:
        await self.real.aclose()
        await self.synthetic.aclose()
        for server in self.servers:
            server.close()
            await server.wait_closed()

"""

STALL_ROUTES = """    elif path == "/stalled-asset":
        body = body.replace("</head>", '<script src="/stalled.js"></script></head>')
"""
