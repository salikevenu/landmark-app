import firebase_admin
from firebase_admin import credentials, auth as firebase_auth
import os
import json

# Initialize Firebase Admin
# Option 1: Using service account JSON file (local development)
if os.path.exists("firebase-service-account.json"):
    cred = credentials.Certificate("firebase-service-account.json")
    firebase_admin.initialize_app(cred)
else:
    # Option 2: Using environment variable (production on Render)
    firebase_creds = os.getenv("FIREBASE_SERVICE_ACCOUNT")
    if firebase_creds:
        cred_dict = json.loads(firebase_creds)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
    else:
        print("Firebase credentials not found")