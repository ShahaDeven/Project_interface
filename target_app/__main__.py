"""`python -m target_app` — runs the portal on http://127.0.0.1:5000"""

from .app import serve

if __name__ == "__main__":
    serve()
