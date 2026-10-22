"""Query Farm's shared Google *Desktop app* OAuth registration.

Query Farm sets these identifiers before distributing the worker. Every user
then signs in with their own Google account through the same app. Desktop apps
are public clients: this client secret is not a confidential web-server secret.
Never put user tokens here. No per-user app registration or configuration is needed.
"""

CLIENT_ID = "88702451095-8fgu2qqbdk2evs1ja28rjl5dp0fs1tmn.apps.googleusercontent.com"
CLIENT_SECRET = "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"
