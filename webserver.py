import os
from flask import Flask


def create_app() -> Flask:
    app = Flask(__name__)

    @app.route("/", methods=["GET", "HEAD"])
    def home():
        return "OK", 200

    return app


if __name__ == "__main__":
    app = create_app()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
