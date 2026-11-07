def main() -> None:
    """Launch the API and tracker with the faster command."""
    import os
    import uvicorn
    from . import config

    uvicorn.run("faster.app:app", host=os.getenv("HOST", "127.0.0.1"),
                port=int(os.getenv("PORT", "8000")))
