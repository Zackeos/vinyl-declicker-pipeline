import os

from dotenv import load_dotenv

load_dotenv()

from waitress import serve  # noqa: E402

from app import app  # noqa: E402  (starts the audio engine and panel listener)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    print(f"Vinyl Processor web UI on http://0.0.0.0:{port}", flush=True)
    serve(app, host='0.0.0.0', port=port, threads=6, ident=None)
