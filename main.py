import logging
import os

import uvicorn

from managed_agents.api import create_app


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    uvicorn.run(create_app(), host=os.environ.get("HOST", "127.0.0.1"),
                port=int(os.environ.get("PORT", "8080")), access_log=False)


if __name__ == "__main__":
    main()
