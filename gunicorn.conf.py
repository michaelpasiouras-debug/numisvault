"""Shared production settings, including Render's bare gunicorn command.

Keep these defaults aligned with Procfile. A synchronous 30-second worker
cannot serve the independent market/specification requests reliably.
"""
import os

bind = "0.0.0.0:" + os.environ.get("PORT", "8000")
workers = 1
worker_class = "gthread"
threads = 8
timeout = 90
accesslog = "-"
errorlog = "-"
