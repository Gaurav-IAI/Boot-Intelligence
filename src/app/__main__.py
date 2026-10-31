from .cli.main import app

if __name__ == "__main__":      # worker processes (OCR pool) import this module too
    app()
