import os

# Render requires binding on 0.0.0.0:$PORT.
# This file is used when start command includes: -c gunicorn.conf.py
bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
workers = 1
threads = 2
timeout = 120
accesslog = "-"
errorlog = "-"
loglevel = "info"
capture_output = True
