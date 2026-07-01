# TaoLite Goods Listing Flask Demo

## Run

Install dependencies first and make sure Flask is available in the environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
PORT=8082 python app.py
```

Open `http://localhost:8082` in your browser.

## Run with Docker

```bash
docker build -t products-flask-demo .
docker run --rm -p 8082:8082 products-flask-demo
```
