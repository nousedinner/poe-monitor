import os
from app import create_app

app = create_app()

if __name__ == "__main__":
    from waitress import serve

    serve(
        app,
        host=os.environ.get("API_BIND", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
        threads=8,
        channel_timeout=120,
        max_request_body_size=256 * 1024,
    )
