# Vercel serverless entry point. Vercel's @vercel/python runtime imports `app`
# from this file and serves it as a WSGI application.
#
# The Flask app itself lives in /app.py so that local `python app.py` still works.
from app import app
