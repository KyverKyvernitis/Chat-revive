import asyncio
from bot import client, start_discord
from webserver import start_http_server

async def main():
    await start_http_server()
    await start_discord()

if __name__ == "__main__":
    asyncio.run(main())
