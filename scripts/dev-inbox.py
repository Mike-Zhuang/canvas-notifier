"""本机开发 SMTP 收件器，原始邮件仅保存在被忽略的 .local/mail 中。"""

import asyncio
import signal
import uuid
from pathlib import Path

from aiosmtpd.controller import Controller


class Inbox:
    async def handle_DATA(self, server, session, envelope):
        directory = Path(".local/mail")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / (uuid.uuid4().hex + ".eml")
        path.write_bytes(envelope.content)
        path.chmod(0o600)
        return "250 Accepted by local development inbox"


async def main():
    controller = Controller(Inbox(), hostname="127.0.0.1", port=1025)
    controller.start()
    event = asyncio.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(signum, event.set)
    print("Development SMTP: 127.0.0.1:1025; messages: .local/mail/")
    try:
        await event.wait()
    finally:
        controller.stop()


if __name__ == "__main__":
    asyncio.run(main())
